"""Read workspace content built from Nex's existing archive and scheduled work."""

import hashlib
import json

from .conversation_history import SHARED_QUESTION, TURN_ID
from .goal_follow_ups import for_messages
from .goals import Goals
from .pdf import artifacts as pdf_artifacts
from .store import CURRENT_EVIDENCE
from .workspace_chat_pages import GOAL_NOTICES, GOAL_UPDATES, chat_message
from .workspace_ideas import ideas

__all__ = ["artifacts", "chat_history", "chat_snapshot", "edition_artifact", "ideas"]


def chat_history(store):
    with store.connect() as db:
        db.execute("BEGIN")
        exchanges = db.execute(
            f"""SELECT {TURN_ID} AS id,q.body AS question,q.started AS asked,
                       m.body AS answer,m.started AS answered,m.sources
                FROM memory q LEFT JOIN memory m ON m.id='a-'||{TURN_ID} AND m.kind='answer'
                WHERE {SHARED_QUESTION} AND {CURRENT_EVIDENCE}
                ORDER BY q.started DESC,q.number DESC LIMIT 30""",
        ).fetchall()
        updates = db.execute(
            f"SELECT * FROM ({GOAL_UPDATES} UNION ALL {GOAL_NOTICES}) ORDER BY date DESC,id DESC LIMIT 30",
            {"date": "9999"},
        ).fetchall()
        follow_ups = for_messages(db, [row["id"] for row in updates])
    messages = []
    for row in exchanges:
        identifier = row["id"]
        messages.append({"id": "q-" + identifier, "role": "user", "text": row["question"],
                         "date": row["asked"], "sources": []})
        if row["answer"] is not None:
            messages.append({"id": "a-" + identifier, "role": "assistant", "text": row["answer"],
                             "date": max(row["asked"], row["answered"]),
                             "sources": [source for source in json.loads(row["sources"]) if source != identifier]})
    messages.extend(chat_message(row, follow_ups.get(row["id"])) for row in updates)
    return sorted(messages, key=lambda item: (item["date"], item["role"] != "user", item["id"]))


def chat_snapshot(store, cursor):
    messages = chat_history(store)
    version = hashlib.sha256(json.dumps(messages, sort_keys=True).encode()).hexdigest()
    if cursor == version:
        return {"cursor": version}
    return {"cursor": version, "items": messages}


def artifacts(store, goal=None):
    with store.connect() as db:
        db.execute("BEGIN")
        if goal:
            Goals.find(db, goal)
            editions = []
        else:
            editions = db.execute("SELECT * FROM feed_editions ORDER BY day DESC LIMIT 30").fetchall()
        updates = db.execute(
            "SELECT a.id,a.created,a.title,a.detail,a.goal_id,g.title AS goal "
            "FROM goal_activity a JOIN goals g ON a.goal_id=g.id WHERE a.artifact=1 "
            "AND (? IS NULL OR a.goal_id=?) AND NOT EXISTS (SELECT 1 FROM pdf_artifacts p WHERE p.id=a.id) "
            "ORDER BY a.created DESC,a.rowid DESC LIMIT 50", (goal, goal),
        ).fetchall()
        files = pdf_artifacts.files(db, goal)
    files.extend(edition_artifact(row) for row in editions)
    files.extend({
        "id": row["id"], "title": row["goal"], "subtitle": row["title"], "date": row["created"], "kind": "goal",
        "text": row["detail"], "goal_id": row["goal_id"],
    } for row in updates)
    return sorted(files, key=lambda item: item["date"], reverse=True)


def edition_artifact(row):
    title = "Daily news · " + row["day"]
    sections = [title]
    for story in json.loads(row["stories"]):
        sections.append("\n".join((story["title"], story["source"], story["summary"], story["url"])))
    return {"id": "feed-" + row["day"], "title": title, "subtitle": "Feed edition",
                "date": row["generated"], "kind": "feed", "text": "\n\n".join(sections)}
