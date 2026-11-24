"""The transcript timeline and recall scope for one local calendar day."""

from __future__ import annotations

import json
import re
from bisect import bisect_right
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from uuid import UUID

from .activities.recall import activity_question, evidence_for_periods
from .activities.timeline import timeline
from .passages import matching_passage
from .recall_dates import recall_dates, time_zone
from .response_language import reply_language
from .store import excerpt, normalized, now, search_prefixes
from .workspace_features import enabled

CONVERSATION_GAP = timedelta(seconds=90)
CONVERSATION_LIMIT = timedelta(minutes=20)
PENDING = {"queued", "processing", "awaiting_transcription"}
RECAP = re.compile(
    r"\b(?:(?:summari[sz]e|recap|(?:give me )?(?:a |the )?summary of) "
    r"(?:(?:my|our) day(?: (?:today|yesterday))?|today|yesterday)|how did my day go|"
    r"what did (?:we|i) (?:discuss|talk about|do) (?:today|yesterday)|"
    r"(?:bugun|dun) (?:ne(?:ler)? (?:konustuk|yaptik|yaptim)|nasil gecti)|"
    r"(?:gunumu|bugunu|dunu) ozetle)(?: please| lutfen)?[.,?! ]*$"
)


def recap_context(query, zone, reference=None):
    if not RECAP.search(normalized(query)):
        return None
    scope = recall_dates(query, zone, reference) or recall_dates("today", zone, reference)
    return DayContext(scope.local_dates[0], zone)


@dataclass(frozen=True)
class DayContext:
    day: str
    zone: str
    source: str | None = None
    focus: str | None = None

    @classmethod
    def parse(cls, day, zone, source=None, focus=None):
        if not isinstance(day, str) or date.fromisoformat(day).isoformat() != day:
            raise ValueError("Choose a Daybook date in YYYY-MM-DD format.")
        zone = time_zone(zone).key
        scope = recall_dates(day, zone)
        if scope is None:
            raise ValueError("Choose a valid Daybook date.")
        if source is not None and (not isinstance(source, str) or str(UUID(source)) != source):
            raise ValueError("Choose a valid Daybook source.")
        if focus is not None and (source is None or not isinstance(focus, str) or not 1 <= len(focus) <= 320):
            raise ValueError("Choose a valid Daybook highlight.")
        return cls(day, zone, source, focus)

    @property
    def bounds(self):
        return recall_dates(self.day, self.zone).bounds

    @property
    def local_dates(self):
        return recall_dates(self.day, self.zone).local_dates


@dataclass
class Conversation:
    rows: list
    started: datetime
    ended: datetime

    def add(self, row, ended):
        self.rows.append(row)
        self.ended = max(self.ended, ended)

    def public(self):
        first = self.rows[0]
        return {"id": first["id"], "started": first["started"],
                "ended": self.ended.isoformat(timespec="milliseconds"), "device": first["device"],
                "mode": first["mode"], "count": len(self.rows), "preview": excerpt(first["transcript"], 160)}


def group_recordings(rows, gaps=()):
    conversations, active, last_gap = [], {}, {}
    for gap in gaps:
        last_gap.setdefault(gap["device"], []).append(gap["started"])
    for row in rows:
        device = row["device"]
        started = datetime.fromisoformat(row["started"])
        ended = started + timedelta(seconds=row["duration"])
        group = active.get(device)
        boundaries = last_gap.get(device, [])
        start_boundary = bisect_right(boundaries, group.rows[-1]["started"]) if group else 0
        crosses_gap = start_boundary < len(boundaries) and boundaries[start_boundary] <= row["started"]
        if (group is None or row["mode"] != group.rows[0]["mode"] or started - group.ended > CONVERSATION_GAP
                or started - group.started >= CONVERSATION_LIMIT
                or crosses_gap):
            group = active[device] = Conversation([], started, ended)
            conversations.append(group)
        group.add(row, ended)
    return conversations


class Daybook:
    def __init__(self, store):
        self.store = store

    def recordings(self, context, *, pending=False):
        states = "('ready','queued','processing','awaiting_transcription')" if pending else "('ready')"
        with self.store.connect() as db:
            return db.execute(
                f"""SELECT * FROM recordings WHERE started>=? AND started<? AND state IN {states}
                    ORDER BY started, id""", context.bounds,
            ).fetchall()

    def conversations(self, context, rows):
        with self.store.connect() as db:
            gaps = db.execute(
                "SELECT started,device FROM memory WHERE kind='gap' AND started>=? AND started<? ORDER BY started",
                context.bounds,
            ).fetchall()
        return group_recordings([row for row in rows if row["state"] == "ready" and row["transcript"]], gaps)

    def overview(self, context):
        rows = self.recordings(context, pending=True)
        groups = self.conversations(context, rows)
        activities = [item for item in timeline(self.store, context.bounds)["items"] if item["category"] != "unknown"]
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if groups or activities:
                db.execute("INSERT OR IGNORE INTO daybook_reports(day,zone,due) VALUES(?,?,?)",
                           (context.day, context.zone, now()))
            report = db.execute("SELECT due,CASE WHEN with_activities=? THEN briefing END AS briefing "
                                "FROM daybook_reports WHERE day=? AND zone=?",
                                (enabled(db, "activities"), context.day, context.zone)).fetchone()
        return {"day": context.day, "conversations": [group.public() for group in groups], "activities": activities,
                "pending": sum(row["state"] in PENDING for row in rows),
                "briefing": json.loads(report["briefing"]) if report and report["briefing"] else None,
                "updating": bool(report and report["due"])}

    def conversation(self, context):
        for group in self.conversations(context, self.recordings(context)):
            if any(row["id"] == context.source for row in group.rows):
                return group.public() | {"recordings": [self.store.public_recording(row) for row in group.rows]}
        return None

    def briefing(self, context):
        with self.store.connect() as db:
            row = db.execute("SELECT briefing FROM daybook_reports WHERE day=? AND zone=? AND with_activities=?",
                             (context.day, context.zone, enabled(db, "activities"))).fetchone()
        return json.loads(row["briefing"]) if row and row["briefing"] else None

    def ready_recap(self, context, exclude=None):
        with self.store.connect() as db:
            row = db.execute("SELECT briefing FROM daybook_reports WHERE day=? AND zone=? AND due IS NULL "
                             "AND with_activities=?", (context.day, context.zone, enabled(db, "activities"))).fetchone()
        if row and row["briefing"]:
            report = json.loads(row["briefing"])
            if report.get("summary") and report.get("highlights"):
                return {"text": report["summary"], "language": reply_language(report["summary"]), "engine": "daybook",
                        "sources": list(dict.fromkeys(item["source"] for item in report["highlights"]))}
        # Until the day's briefing is ready, quote the available material without filling in missing facts.
        # An activity observation can cite the very same clip as the transcript.
        by_source = {}
        for item in self.evidence(context, "", exclude):
            by_source.setdefault(item["id"], item)
        evidence = list(by_source.values())[:3]
        if evidence:
            text = " ".join(excerpt(item["text"], 210) for item in evidence)
            return {"text": text, "language": reply_language(text), "engine": "archive_search",
                    "sources": list(dict.fromkeys(item["id"] for item in evidence))}
        return None

    def selection(self, context, exclude):
        rows = [row for row in self.recordings(context) if row["transcript"] and row["id"] != exclude]
        if context.source is None:
            return rows, None
        if context.focus:
            clips = timeline(self.store, context.bounds, source=context.source, exclude=exclude)["items"]
            if clips and clips[0]["title"] == context.focus:
                allowed = {clip["id"] for clip in clips}
                return [row for row in rows if row["id"] in allowed], allowed
        for group in self.conversations(context, rows):
            identifiers = {row["id"] for row in group.rows}
            if context.source in identifiers:
                return group.rows, identifiers
        return [], {context.source}

    def evidence(self, context, question, exclude=None):
        rows, allowed = self.selection(context, exclude)
        report = self.briefing(context) if context.source is None else None
        featured = {item["source"] for item in report["highlights"]} if report else set()
        query = question + (" " + context.focus if context.focus else "")
        terms = search_prefixes(query)

        def rank(index):
            row = rows[index]
            text = normalized(row["transcript"])
            return (row["id"] == context.source, sum(term in text for term in terms),
                    row["id"] in featured, -index)

        indices = sorted(range(len(rows)), key=rank, reverse=True)[:5]
        speech = [{"id": row["id"], "started": row["started"], "kind": "recording",
                   "text": matching_passage(row["transcript"], query, limit=540)}
                  for row in (rows[index] for index in indices)]
        periods = timeline(self.store, context.bounds, exclude=exclude, allowed=allowed)["items"]
        activities = evidence_for_periods([p for p in periods if p["category"] != "unknown"],
                                         query, time_zone(context.zone), limit=4)
        return activities + speech if activity_question(query) else speech + activities
