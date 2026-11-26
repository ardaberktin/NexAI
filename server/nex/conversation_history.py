"""The recent, ordinary exchanges shared by Chat and Live Conversation."""

import json
from datetime import UTC, datetime, timedelta

from .store import CURRENT_EVIDENCE, excerpt

MAX_EXCHANGES = 8
RESUME_WINDOW = timedelta(minutes=30)

# Older recorded voice exchanges used the recording itself as the question.
TURN_ID = "CASE q.kind WHEN 'question' THEN substr(q.id,3) ELSE q.id END"
SHARED_QUESTION = """(
    (q.kind='question' AND q.id LIKE 'q-%' AND q.engine IS NULL
     AND NOT EXISTS (SELECT 1 FROM memory a WHERE a.id='a-'||substr(q.id,3) AND a.engine='private_request')) OR
    (q.kind='recording' AND EXISTS (SELECT 1 FROM recordings r WHERE r.id=q.id
     AND r.mode='voice' AND r.live_handled=0 AND r.reply IS NOT NULL
     AND (CASE WHEN json_valid(r.reply) THEN json_extract(r.reply,'$.engine') END) IS NOT 'private_request')
     AND NOT EXISTS (SELECT 1 FROM memory p WHERE p.id='q-'||q.id))
)"""


def time_key(value):
    return value.astimezone(UTC).isoformat(timespec="milliseconds")


def saved_chat_reply(db, identifier, query, device, scope, language):
    row = db.execute(
        """SELECT q.body,q.device,q.engine AS scope,a.body AS answer,a.sources,a.engine
           FROM memory q LEFT JOIN memory a ON a.id=? WHERE q.id=?""",
        ("a-" + identifier, "q-" + identifier),
    ).fetchone()
    if row is None:
        return None
    if (row["body"], row["device"], row["scope"]) != (query, device, scope):
        raise ValueError("This question ID already belongs to a different request.")
    if row["answer"] is not None:
        return {"text": row["answer"], "sources": json.loads(row["sources"]),
                "engine": row["engine"], "language": language}
    return None


def recent_dialogue(store, before=None, *, after=None):
    comparison = "<" if before else "<="
    before = before or datetime.now(UTC)
    lower, since = ">=", before - RESUME_WINDOW
    if after is not None and after >= since:
        lower, since = ">", after
    with store.connect() as db:
        rows = db.execute(
            f"""SELECT q.id,q.started,q.body AS question,m.body AS answer,m.engine,m.sources,m.recall_cursor
                FROM memory q JOIN memory m ON m.id='a-'||{TURN_ID}
                WHERE q.started{lower}? AND q.started{comparison}? AND m.started<=? AND m.kind='answer'
                AND {SHARED_QUESTION} AND {CURRENT_EVIDENCE}
                ORDER BY q.started DESC,q.number DESC LIMIT ?""",
            (time_key(since), time_key(before), time_key(before), MAX_EXCHANGES),
        ).fetchall()
    history = []
    for row in reversed(rows):
        own_clip = row["id"].removeprefix("q-")
        history.extend((
            {"kind": "question", "id": row["id"], "started": row["started"], "text": row["question"]},
            {"kind": "answer", "id": "a-" + own_clip, "text": row["answer"], "engine": row["engine"],
             "recall_cursor": json.loads(row["recall_cursor"]) if row["recall_cursor"] else None,
             "sources": [s for s in json.loads(row["sources"]) if s != own_clip]},
        ))
    return history


def watch_dialogue(store, turn):
    with store.connect() as db:
        rows = db.execute(
            """SELECT transcript, reply FROM recordings WHERE device='watch' AND watch_conversation=?
               AND started<? AND state='ready' ORDER BY started DESC LIMIT ?""",
            (turn["watch_conversation"], turn["started"], MAX_EXCHANGES),
        ).fetchall()
    history = []
    for row in reversed(rows):
        reply = json.loads(row["reply"]) if row["reply"] else {}
        if reply.get("public_voice") is not True or reply.get("engine") == "private_request":
            history.clear()
        else:
            history.extend(({"kind": "question", "text": row["transcript"]}, {"kind": "answer", **reply}))
    return history


def prompt_history(history):
    """Keep the newest context within the local model's input budget, in spoken order."""
    remaining = 3600
    messages = []
    # A canceled utterance never acquired a disclosure decision or a public answer.
    while history and history[-1]["kind"] != "answer":
        history = history[:-1]
    for item in reversed(history[-2 * MAX_EXCHANGES:]):
        content = excerpt(item["text"], 650)
        if item.get("private") and item.get("selected"):
            content += "\nEarlier tools: " + excerpt(item["selected"], 320)
        if len(content) > remaining:
            break
        remaining -= len(content)
        role = "user" if item["kind"] == "question" else "assistant"
        messages.append({"role": role, "content": content})
    messages.reverse()
    return messages[1:] if messages and messages[0]["role"] == "assistant" else messages
