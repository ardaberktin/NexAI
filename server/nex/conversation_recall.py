"""Answer requests for earlier questions using the user's side of the conversation."""

from dataclasses import dataclass
from datetime import UTC, datetime

from .conversation_history import time_key
from .conversation_intent import (
    earlier_question,
    greeting_reply,
    prior_question_limit,
    words,
)
from .recall_dates import recall_dates
from .store import excerpt
from .voice_stream import SpeechChunks
from .wake import ENGLISH_START, LEADING_WAKE, TURKISH_QUESTION, addressed_question


def archive_questions(store, start, end, cursor):
    # Recorded turns with a reply were handled as questions. Older push-to-talk
    # clips are also explicit requests even if they have no saved reply.
    with store.connect() as db:
        return db.execute(
            """WITH turns AS (
                   SELECT id,started,kind,body,engine,CASE WHEN id LIKE 'q-%' THEN substr(id,3) ELSE id END AS turn_id
                   FROM memory WHERE kind IN ('question','recording') AND started>=? AND started<?
               ) SELECT m.id,m.started,CASE WHEN r.origin='manual' THEN r.transcript ELSE m.body END AS body,
                        CASE WHEN m.kind='recording' OR r.origin='manual' THEN r.mode END AS mode,
                        CASE WHEN r.state='ready' THEN r.id ELSE m.id END AS source
               FROM turns m LEFT JOIN recordings r ON r.id=m.turn_id
               WHERE (m.started,m.turn_id)<(?,?) AND (
                   (m.kind='question' AND m.engine IS NULL) OR (m.kind='recording' AND
                   NOT EXISTS (SELECT 1 FROM memory q WHERE q.id='q-'||m.id) AND (
                       (r.mode='voice' AND (r.live_handled=0 OR r.reply IS NOT NULL)) OR
                       (r.mode='ambient' AND r.wake=1 AND r.reply IS NOT NULL)
                   ))
               ) ORDER BY m.started DESC,m.turn_id DESC LIMIT 64""",
            (start, end, cursor["before"] if cursor else end, cursor["turn"] if cursor else ""),
        ).fetchall()


def question_text(text, mode=None):
    if mode == "ambient":
        return addressed_question(text) or ""
    return LEADING_WAKE.sub("", text.strip(), count=1)


def speak_question_reply(result, emit):
    chunks = SpeechChunks(emit)
    speech = result["text"].replace("“", "").replace("”", "")
    chunks.add(speech, result["language"])
    chunks.flush(result["language"], final=True)


def last_recall(history):
    """Keep a reference through a misunderstood correction, until the topic changes."""
    answer = None
    for item in reversed(history):
        if item["kind"] == "answer":
            answer = item
        else:
            if answer and answer.get("recall_cursor"):
                return item, answer
            scope = recall_dates(item["text"])
            if answer and answer.get("engine") == "archive_search" and prior_question_limit(
                    scope.query if scope else item["text"]):
                return item, answer
            if not earlier_question(item["text"], allow_noisy=True):
                break
            answer = None
    return None


def previous_cursor(store, previous, zone):
    question, answer = previous
    if cursor := answer.get("recall_cursor"):
        return cursor
    # Older installations kept the quoted question's source, without a cursor.
    for identifier in reversed(answer.get("sources", [])):
        target = store.source("q-" + identifier.removeprefix("q-")) or store.source(identifier)
        if target and target["kind"] in ("question", "recording"):
            scope = (recall_dates(question["text"], zone, datetime.fromisoformat(question["started"]))
                     if question.get("started") else None)
            return {"before": target["started"], "turn": identifier.removeprefix("q-"),
                    "since": scope.bounds[0] if scope else "", "quoted": words(target["text"])[:260]}
    return None


def candidates_before(store, start, end, cursor, history, exclude):
    rows = archive_questions(store, start, end, cursor)
    candidates = {(r["started"], r["id"].removeprefix("q-")):
                  (r["source"], question_text(r["body"], r["mode"])) for r in rows}
    saved = {key[1] for key in candidates}
    bound = (cursor["before"], cursor["turn"]) if cursor else (end, "")
    for item in history:
        if item["kind"] != "question" or not item.get("id"):
            continue
        key = (item["started"], item["id"].removeprefix("q-"))
        if key[1] not in saved and start <= key[0] < end and key < bound:
            candidates[key] = (None, question_text(item["text"]))
    for key in sorted(candidates, reverse=True):
        if key[1] != exclude:
            yield key, *candidates[key]


def quoted_question(text):
    spoken = excerpt(text, 260).rstrip()
    if spoken[-1] not in ".?!…":
        clean = words(spoken)
        spoken += "?" if ENGLISH_START.match(clean) or TURKISH_QUESTION.search(clean) else "."
    return "“" + spoken + "”"


@dataclass(frozen=True)
class QuestionCandidates:
    count: int
    entries: tuple


def selected_questions(store, count, bounds, history, anchor, exclude, *, before=False, zone="UTC"):
    cursor = None
    if before:
        for index in range(len(history) - 1, 0, -1):
            answer = history[index]
            selected = answer.get("selected_tools", ())
            if (answer.get("recall_cursor") or "past_questions" in selected
                    or (answer.get("engine") == "archive_search" and answer.get("sources"))):
                cursor = previous_cursor(store, (history[index - 1], answer), zone)
                break
        if cursor is None:
            return QuestionCandidates(count, ())
    end = time_key(anchor)
    start = cursor["since"] if cursor else ""
    if bounds:
        start, end = max(start, bounds[0]), min(end, bounds[1])
    found = []
    excluded_quote = cursor.get("quoted") if cursor else None
    for position, identifier, text in candidates_before(store, start, end, cursor, history, exclude):
        key = words(text)[:260]
        if not key or key == excluded_quote:
            continue
        anchor_cursor = {"before": position[0], "turn": position[1], "since": start, "quoted": key}
        found.append((excerpt(text, 300), identifier, anchor_cursor))
        if len(found) == min(16, count + 8):
            break
    return QuestionCandidates(count, tuple(found))


def recent_question_reply(store, query, language, scope, *, asked_at=None, exclude=None, history=(), zone="UTC"):
    count = prior_question_limit(scope.query if scope else query)
    cursor = None
    continuing = not count
    if continuing:
        if not earlier_question(query, allow_noisy=True) or not (previous := last_recall(history)):
            return None
        cursor = previous_cursor(store, previous, zone)
        if not cursor:
            return missing_question(language)
        count = 1
    end = time_key(asked_at or datetime.now(UTC))
    start = cursor["since"] if cursor else (scope.bounds[0] if scope else "")
    if scope:
        end = min(end, scope.bounds[1])
    quotes, sources = [], []
    seen = {cursor.get("quoted")} if cursor else set()
    for position, identifier, text in candidates_before(store, start, end, cursor, history, exclude):
        key = words(text)[:260]
        if (not key or key in seen or key in {"yes", "no", "okay", "yes please", "nex", "neks", "next"}
                or greeting_reply(text, "auto") or earlier_question(text, allow_noisy=True)):
            continue
        earlier_scope = recall_dates(text)
        if prior_question_limit(earlier_scope.query if earlier_scope else text):
            continue
        seen.add(key)
        quotes.append(quoted_question(text))
        cursor = {"before": position[0], "turn": position[1], "since": start, "quoted": key[:260]}
        if identifier:
            sources.append(identifier)
        if len(quotes) == count:
            break

    if not quotes:
        return missing_question(language, cursor)
    if len(quotes) == 1 and language == "en":
        text = ("Before that, you asked, " if continuing else "You asked, ") + quotes[0]
    else:
        heading = "Daha önce şunları sordun." if language == "tr" else "Here’s what you asked earlier."
        text = "\n\n".join([heading, *quotes])
    return {"text": text, "language": language, "sources": sources, "engine": "archive_search", "recall_cursor": cursor}


def missing_question(language, cursor=None):
    text = ("Daha önce sorduğun bir soru bulamadım." if language == "tr"
            else "I couldn’t find an earlier question from you.")
    result = {"text": text, "language": language, "sources": [], "engine": "archive_search"}
    if cursor:
        result["recall_cursor"] = cursor
    return result
