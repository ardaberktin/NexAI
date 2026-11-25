import hashlib
import json
import re
import sqlite3
import unicodedata
from contextlib import contextmanager
from datetime import UTC, datetime
from functools import cached_property
from pathlib import Path

from .activities.schema import attach_screen
from .activities.schema import initialize as initialize_activities
from .audio_archive import AudioArchive
from .daybook_schedule import schedule_recordings
from .mcp.connections import SCHEMA as MCP_SCHEMA
from .privacy.state import PrivacyState
from .workspace_features import SCHEMA as FEATURES_SCHEMA
from .workspace_features import snapshot as feature_snapshot
from .workspace_schema import WORKSPACE_SCHEMA, migrate_workspace

SCHEMA = """
CREATE TABLE IF NOT EXISTS public_chat(id TEXT PRIMARY KEY, device TEXT, question TEXT, result TEXT);
CREATE TABLE IF NOT EXISTS recordings (
    id TEXT PRIMARY KEY, device TEXT NOT NULL, started TEXT NOT NULL,
    duration REAL NOT NULL, sha TEXT NOT NULL, size INT NOT NULL,
    mode TEXT NOT NULL, wake INT NOT NULL, language TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'queued', error TEXT, transcript TEXT,
    origin TEXT, notes TEXT, reply TEXT, updated TEXT NOT NULL,
    time_zone TEXT NOT NULL DEFAULT 'UTC'
);
CREATE TABLE IF NOT EXISTS memory (
    number INTEGER PRIMARY KEY, id TEXT UNIQUE NOT NULL, started TEXT NOT NULL,
    kind TEXT NOT NULL, device TEXT NOT NULL, body TEXT NOT NULL, indexed TEXT NOT NULL,
    sources TEXT NOT NULL DEFAULT '[]', engine TEXT
);
CREATE TABLE IF NOT EXISTS transcript_revisions (
    number INTEGER PRIMARY KEY,
    recording_id TEXT NOT NULL,
    request_id TEXT UNIQUE NOT NULL,
    editor TEXT NOT NULL,
    changed TEXT NOT NULL,
    previous_text TEXT,
    previous_origin TEXT,
    text TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS transcript_revision_history ON transcript_revisions(recording_id, number DESC);
CREATE INDEX IF NOT EXISTS memory_started ON memory(started DESC, number DESC);
CREATE INDEX IF NOT EXISTS memory_chat ON memory(started DESC,number DESC) WHERE kind IN ('question','answer');
CREATE INDEX IF NOT EXISTS memory_question_chat_page ON memory(started DESC, id DESC) WHERE kind='question';
CREATE INDEX IF NOT EXISTS memory_answer_chat_page ON memory(started DESC, id DESC) WHERE kind='answer';
CREATE INDEX IF NOT EXISTS recordings_state ON recordings(state, mode, started);
CREATE INDEX IF NOT EXISTS recordings_device ON recordings(device, started DESC);
CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(
    indexed, content='memory', content_rowid='number', tokenize='unicode61 remove_diacritics 2'
);
CREATE TRIGGER IF NOT EXISTS memory_insert AFTER INSERT ON memory BEGIN
    INSERT INTO memory_fts(rowid, indexed) VALUES (new.number, new.indexed);
END;
CREATE TRIGGER IF NOT EXISTS memory_update AFTER UPDATE OF indexed ON memory BEGIN
    INSERT INTO memory_fts(memory_fts, rowid, indexed) VALUES ('delete', old.number, old.indexed);
    INSERT INTO memory_fts(rowid, indexed) VALUES (new.number, new.indexed);
END;
CREATE TRIGGER IF NOT EXISTS memory_delete AFTER DELETE ON memory BEGIN
    INSERT INTO memory_fts(memory_fts, rowid, indexed) VALUES ('delete', old.number, old.indexed);
END;
"""

STOP = {
    "about", "and", "are", "did", "does", "for", "from", "had", "have", "how", "into", "just", "nex",
    "our", "tell", "that", "the", "them", "then", "there", "they", "this", "was", "were", "what",
    "when", "where", "which", "who", "why", "will", "with", "would", "you", "your", "bir", "bu",
    "da", "de", "diye", "için", "ile", "ki", "kim", "mi", "mı", "mu", "mü", "ne", "neden",
    "nerede", "nasıl", "oldu", "olarak", "olan", "ve", "veya", "var", "çok",
}

CURRENT_EVIDENCE = """
    (m.kind <> 'answer' OR NOT EXISTS (
        SELECT 1 FROM json_each(m.sources) AS source
        JOIN transcript_revisions AS revision ON revision.recording_id=source.value
        WHERE revision.changed>=m.started
    ))
"""
MEMORY_COLUMNS = "m.*,(SELECT audio_state FROM recordings WHERE id=m.id) AS audio_state"
DISTINCT_NOTE = """(m.kind <> 'note' OR NOT EXISTS (
    SELECT 1 FROM memory AS recording
    WHERE recording.id=substr(m.id,3) AND recording.kind='recording' AND recording.body=m.body
))"""


def now():
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def normalized(value):
    value = unicodedata.normalize("NFKD", value.casefold().replace("ı", "i"))
    return "".join(char for char in value if not unicodedata.combining(char))


def query_terms(value):
    words = re.findall(r"[^\W_]+", normalized(value), re.UNICODE)
    return list(dict.fromkeys(word for word in words if word not in STOP and len(word) >= 2))[:18]


def search_prefixes(value):
    terms = query_terms(value)
    prefixes = []
    for term in terms:
        if len(term) > 5 and term.endswith("ies"):
            prefixes.append(term[:-3] + "y")
        elif len(term) > 4 and term.endswith(("ches", "shes", "xes", "zes", "sses")):
            prefixes.append(term[:-2])
        elif len(term) > 4 and term.endswith("s") and not term.endswith(("ss", "us", "is")):
            prefixes.append(term[:-1])
        else:
            prefixes.append(term)
    return list(dict.fromkeys(prefixes))


def excerpt(value, limit=700):
    return value if len(value) <= limit else value[:limit].rstrip() + "…"


class ArchiveFull(Exception):
    pass


class RecordingConflict(Exception):
    pass


class TranscriptConflict(Exception):
    pass


LIVE_SCHEMA = """
CREATE INDEX IF NOT EXISTS legacy_voice_history ON recordings(started DESC, id DESC)
    WHERE mode='voice' AND live_handled=0 AND reply IS NOT NULL;
CREATE TABLE IF NOT EXISTS live_turn_results (
    id TEXT PRIMARY KEY, device TEXT NOT NULL, transcript TEXT, reply TEXT,
    updated TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS weather_locations (
    device TEXT PRIMARY KEY, place TEXT NOT NULL
);
"""

PERSONAL_SCHEMA = """
CREATE TABLE IF NOT EXISTS personal_events (
    id TEXT PRIMARY KEY,kind TEXT NOT NULL,text TEXT NOT NULL,created TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS personal_events_kind_time ON personal_events(kind,created,id);
CREATE TABLE IF NOT EXISTS personal_learning (initialized INTEGER PRIMARY KEY CHECK(initialized=1));
"""


DAYBOOK_SCHEMA = """
CREATE INDEX IF NOT EXISTS recordings_chronology ON recordings(started, id);
CREATE INDEX IF NOT EXISTS recordings_journal_changes ON recordings(updated, id)
    WHERE state='ready' AND transcript IS NOT NULL;
CREATE TABLE IF NOT EXISTS daybook_reports (
    day TEXT NOT NULL, zone TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 1,
    due TEXT, briefing TEXT, with_activities INTEGER NOT NULL DEFAULT 1, PRIMARY KEY(day, zone)
);
CREATE INDEX IF NOT EXISTS daybook_due ON daybook_reports(due) WHERE due IS NOT NULL;
CREATE TABLE IF NOT EXISTS daybook_batches (
    digest TEXT PRIMARY KEY, result TEXT NOT NULL, created TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS daybook_scan (
    singleton INTEGER PRIMARY KEY CHECK(singleton=1), updated TEXT NOT NULL, id TEXT NOT NULL
);
"""


class Store:
    @cached_property
    def privacy(self):
        return PrivacyState(self.root)

    def __init__(self, root, min_free, quota, *, ssd_audio=None):
        self.root = Path(root)
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.audio = self.root / "audio"
        self.audio.mkdir(mode=0o700, exist_ok=True)
        self.archive = AudioArchive(self.connect, self.audio, min_free, ssd_audio)
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript(SCHEMA)
            db.execute("BEGIN IMMEDIATE")
            if "time_zone" not in {column["name"] for column in db.execute("PRAGMA table_info(recordings)")}:
                db.execute("ALTER TABLE recordings ADD COLUMN time_zone TEXT NOT NULL DEFAULT 'UTC'")
            if "live_handled" not in {column["name"] for column in db.execute("PRAGMA table_info(recordings)")}:
                db.execute("ALTER TABLE recordings ADD COLUMN live_handled INTEGER NOT NULL DEFAULT 0")
            if "watch_conversation" not in {column["name"] for column in db.execute("PRAGMA table_info(recordings)")}:
                db.execute("ALTER TABLE recordings ADD COLUMN watch_conversation TEXT")
            db.execute("CREATE INDEX IF NOT EXISTS watch_turns ON recordings(watch_conversation, started) "
                       "WHERE watch_conversation IS NOT NULL")
            if "recall_cursor" not in {column["name"] for column in db.execute("PRAGMA table_info(memory)")}:
                db.execute("ALTER TABLE memory ADD COLUMN recall_cursor TEXT")
            self.archive.initialize(db, quota)
        with self.connect() as db:
            db.executescript(LIVE_SCHEMA + DAYBOOK_SCHEMA + PERSONAL_SCHEMA + WORKSPACE_SCHEMA + FEATURES_SCHEMA + MCP_SCHEMA)
            initialize_activities(db)
            migrate_workspace(db, self.root)
            if "with_activities" not in {column["name"] for column in db.execute("PRAGMA table_info(daybook_reports)")}:
                db.execute("ALTER TABLE daybook_reports ADD COLUMN with_activities INTEGER NOT NULL DEFAULT 1")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.root / "memory.sqlite", timeout=15)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def audio_path(self, identifier):
        return self.archive.path(identifier)

    def save_recording(self, identifier, device, started, duration, mode, wake, language, audio, zone="UTC", *,
                       live_handled=False, watch_conversation=None):
        sha = hashlib.sha256(audio).hexdigest()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            previous = db.execute("SELECT * FROM recordings WHERE id=?", (identifier,)).fetchone()
            identity = (device, started, duration, mode, wake, language, sha, zone, live_handled, watch_conversation)
            if previous:
                if tuple(previous[key] for key in (
                    "device", "started", "duration", "mode", "wake", "language", "sha", "time_zone", "live_handled",
                    "watch_conversation"
                )) != identity:
                    raise RecordingConflict
                return self.public_recording(previous), False
            capacity = self.archive.capacity(db)
            if len(audio) > capacity["remaining_bytes"]:
                raise ArchiveFull
            on_ssd = self.archive.write(identifier, audio, capacity)
            db.execute(
                """INSERT INTO recordings(id,device,started,duration,mode,wake,language,sha,time_zone,live_handled,
                   watch_conversation,size,updated,audio_on_ssd) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (identifier, *identity, len(audio), now(), on_ssd),
            )
            row = db.execute("SELECT * FROM recordings WHERE id=?", (identifier,)).fetchone()
            attach_screen(db, row)
            return self.public_recording(row), True

    @staticmethod
    def public_recording(row):
        result = {key: row[key] for key in (
            "id", "device", "started", "duration", "mode", "language", "state", "error",
            "transcript", "origin", "notes", "updated",
        )}
        result["audio_available"] = row["audio_state"] == "available"
        result["audio_removed"] = row["audio_removed"]
        result["reply"] = json.loads(row["reply"]) if row["reply"] else None
        return result

    def recording(self, identifier):
        with self.connect() as db:
            row = db.execute("SELECT * FROM recordings WHERE id=?", (identifier,)).fetchone()
            return self.public_recording(row) if row else None

    def neighbor(self, device, started, before):
        operator, order = ("<", "DESC") if before else (">", "ASC")
        with self.connect() as db:
            return db.execute(
                f"""SELECT * FROM recordings WHERE device=? AND started {operator} ?
                    ORDER BY started {order}, id {order} LIMIT 1""",
                (device, started),
            ).fetchone()

    @staticmethod
    def transcript_snapshot(db, row):
        revisions = db.execute(
            "SELECT number,changed,editor,previous_text,previous_origin FROM transcript_revisions "
            "WHERE recording_id=? ORDER BY number DESC LIMIT 30", (row["id"],),
        ).fetchall()
        return {
            "text": row["transcript"], "origin": row["origin"], "state": row["state"],
            "revision": revisions[0]["number"] if revisions else 0,
            "history": [dict(item) for item in revisions],
        }

    def transcript(self, identifier):
        with self.connect() as db:
            db.execute("BEGIN")
            row = db.execute("SELECT * FROM recordings WHERE id=?", (identifier,)).fetchone()
            return self.transcript_snapshot(db, row) if row else None

    def correct_transcript(self, identifier, editor, request_id, base_revision, value, note, *, engine=None):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM recordings WHERE id=?", (identifier,)).fetchone()
            if row is None:
                return None
            replay = db.execute(
                "SELECT recording_id,editor,text FROM transcript_revisions WHERE request_id=?", (request_id,),
            ).fetchone()
            if replay:
                if tuple(replay) != (identifier, editor, value):
                    raise TranscriptConflict("This edit ID belongs to a different correction.")
                return self.transcript_snapshot(db, row)
            latest = db.execute(
                "SELECT MAX(number) FROM transcript_revisions WHERE recording_id=?", (identifier,),
            ).fetchone()[0] or 0
            if latest != base_revision:
                raise TranscriptConflict("The transcript changed on another device. Reload it before saving.")
            if row["state"] in ("queued", "processing"):
                raise TranscriptConflict("The clip is still processing. Open it again once it is ready.")
            if row["transcript"] == value:
                return self.transcript_snapshot(db, row)
            db.execute(
                """INSERT INTO transcript_revisions(recording_id,request_id,editor,changed,previous_text,
                   previous_origin,text) VALUES(?,?,?,?,?,?,?)""",
                (identifier, request_id, editor, now(), row["transcript"], row["origin"], value),
            )
            self.add_memory(db, identifier, row["started"], "recording", row["device"], value)
            if note:
                self.add_memory(db, "n-" + identifier, row["started"], "note", row["device"], note,
                                [identifier], engine)
            else:
                db.execute("DELETE FROM memory WHERE id=?", ("n-" + identifier,))
            db.execute(
                "UPDATE recordings SET transcript=?,origin='manual',notes=?,state='ready',error=NULL WHERE id=?",
                (value, note, identifier),
            )
            schedule_recordings(db, [row])
            row = db.execute("SELECT * FROM recordings WHERE id=?", (identifier,)).fetchone()
            return self.transcript_snapshot(db, row)

    def add_memory(self, db, identifier, started, kind, device, body, sources=(), engine=None, *, recall_cursor=None):
        db.execute(
            """INSERT INTO memory(id,started,kind,device,body,indexed,sources,engine,recall_cursor)
               VALUES(?,?,?,?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET body=excluded.body,indexed=excluded.indexed,
                   sources=excluded.sources,engine=excluded.engine,recall_cursor=excluded.recall_cursor""",
            (identifier, started, kind, device, body, normalized(body), json.dumps(sources), engine,
             json.dumps(recall_cursor) if recall_cursor else None),
        )

    @staticmethod
    def memory_row(row):
        result = {
            "id": row["id"], "kind": row["kind"], "started": row["started"],
            "device": row["device"], "text": row["body"], "sources": json.loads(row["sources"]),
            "engine": row["engine"],
        }
        if "audio_state" in row.keys() and row["audio_state"] is not None:  # noqa: SIM118 - sqlite3.Row iterates values.
            result["audio_available"] = row["audio_state"] == "available"
        if row["kind"] == "web":
            metadata = json.loads(row["engine"])
            result.update(web_url=metadata["url"], web_title=metadata["title"], web_links=metadata["links"],
                          engine="web_lookup")
        return result

    def source(self, identifier):
        with self.connect() as db:
            row = db.execute(f"SELECT {MEMORY_COLUMNS} FROM memory m WHERE m.id=?", (identifier,)).fetchone()
            if row:
                return self.memory_row(row)
            clip = db.execute("SELECT * FROM recordings WHERE id=?", (identifier,)).fetchone()
            if clip:
                text = clip["transcript"] or clip["error"] or (
                    "No speech was transcribed." if clip["state"] == "quiet" else "Transcript is not available yet.")
                return {"id": identifier, "kind": "recording", "started": clip["started"],
                        "device": clip["device"], "text": text, "sources": [], "engine": None,
                        "audio_available": clip["audio_state"] == "available"}
            return None

    def search(self, query, limit=24, day=None, *, evidence_only=False, exclude=None, bounds=None):
        terms = search_prefixes(query)
        if query and not terms:
            terms = re.findall(r"[^\W_]+", normalized(query), re.UNICODE)[:8]
        filters = ["m.kind <> 'web'", "(? IS NULL OR substr(m.started,1,10)=?)",
                   "(? IS NULL OR m.id<>?)", DISTINCT_NOTE]
        params = (day, day, exclude, exclude)
        if bounds:
            filters.extend(("m.started>=?", "m.started<?"))
            params += bounds
        if evidence_only:
            filters.extend(("m.kind IN ('recording','question','answer')",
                            "(m.kind <> 'question' OR m.indexed <> ?)", CURRENT_EVIDENCE))
            params += (normalized(query),)
        where = " AND ".join(filters)
        params += (limit,)
        with self.connect() as db:
            if not terms:
                if query:
                    return []
                rows = db.execute(
                    f"SELECT {MEMORY_COLUMNS} FROM memory m WHERE {where} "
                    "ORDER BY m.started DESC, m.number DESC LIMIT ?",
                    params,
                )
            else:
                match = " OR ".join(f'"{word}"*' for word in terms)
                rows = db.execute(
                    f"""SELECT {MEMORY_COLUMNS} FROM memory_fts JOIN memory m ON m.number=memory_fts.rowid
                        WHERE memory_fts MATCH ? AND {where}
                        ORDER BY bm25(memory_fts), m.started DESC, m.number DESC LIMIT ?""",
                    (match, *params),
                )
            return [self.memory_row(row) for row in rows]

    def recent_chat(self):
        with self.connect() as db:
            rows = db.execute(
                f"SELECT m.* FROM memory m WHERE m.kind IN ('question','answer') AND {CURRENT_EVIDENCE} "
                "ORDER BY m.started DESC, m.number DESC LIMIT 6"
            )
            return [self.memory_row(row) for row in rows]

    def recordings(self, before, limit=35):
        with self.connect() as db:
            rows = db.execute(
                """SELECT * FROM recordings WHERE started < ? OR (started = ? AND id < ?)
                   ORDER BY started DESC,id DESC LIMIT ?""", (before[0], before[0], before[1], limit),
            )
            return [self.public_recording(row) for row in rows]

    def ready_replies(self, device, since):
        with self.connect() as db:
            rows = db.execute(
                """SELECT * FROM recordings WHERE device=? AND reply IS NOT NULL AND updated>? AND live_handled=0
                   ORDER BY updated LIMIT 50""", (device, since),
            )
            return [self.public_recording(row) for row in rows]

    def status(self):
        with self.connect() as db:
            counts = dict(db.execute("SELECT state,count(*) FROM recordings GROUP BY state").fetchall())
            oldest = db.execute("SELECT min(started) FROM recordings WHERE state IN "
                                "('queued','processing','awaiting_transcription')").fetchone()[0]
            return {"recordings": counts, "oldest_pending": oldest,
                    "workspace_features": feature_snapshot(db)} | self.archive.capacity(db)
