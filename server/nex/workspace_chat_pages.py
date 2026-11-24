"""Pages through the shared chat timeline using stable, chronological boundaries."""

import hashlib
import json

from .conversation_history import SHARED_QUESTION, TURN_ID
from .goal_follow_ups import for_messages
from .store import CURRENT_EVIDENCE

PAGE_SIZE = 40

QUESTIONS = f"""
    SELECT CASE WHEN role.rank=0 THEN q.id ELSE m.id END AS id, q.started AS date, role.rank,
           CASE WHEN role.rank=0 THEN q.body ELSE m.body END AS text,
           CASE WHEN role.rank=0 THEN '[]' ELSE m.sources END AS sources,
           NULL AS title, NULL AS goal_id
    FROM memory q LEFT JOIN memory m ON m.id='a-'||{TURN_ID} AND m.kind='answer'
    CROSS JOIN (SELECT 0 AS rank UNION ALL SELECT 1) role
    WHERE q.kind='question' AND q.started<=:date AND (role.rank=0 OR m.started<q.started)
          AND {SHARED_QUESTION} AND {CURRENT_EVIDENCE}
"""

ANSWERS = f"""
    SELECT m.id, m.started AS date, 1 AS rank, m.body AS text, m.sources, NULL AS title, NULL AS goal_id
    FROM memory m JOIN memory q ON q.id='q-'||substr(m.id,3) AND q.kind='question'
    WHERE m.kind='answer' AND m.id LIKE 'a-%' AND m.started<=:date AND q.started<=m.started
          AND {SHARED_QUESTION} AND {CURRENT_EVIDENCE}
"""

LEGACY_VOICE = f"""
    SELECT CASE WHEN role.rank=0 THEN 'q-'||q.id ELSE m.id END AS id,
           CASE WHEN role.rank=0 THEN q.started ELSE max(q.started,m.started) END AS date, role.rank,
           CASE WHEN role.rank=0 THEN q.body ELSE m.body END AS text,
           CASE WHEN role.rank=0 THEN '[]' ELSE m.sources END AS sources,
           NULL AS title, NULL AS goal_id
    FROM recordings r CROSS JOIN memory q ON q.id=r.id AND q.kind='recording'
    LEFT JOIN memory m ON m.id='a-'||q.id AND m.kind='answer'
    CROSS JOIN (SELECT 0 AS rank UNION ALL SELECT 1) role
    WHERE r.mode='voice' AND r.live_handled=0 AND r.reply IS NOT NULL AND q.started<=:date
          AND (role.rank=0 OR m.started<=:date) AND {SHARED_QUESTION} AND {CURRENT_EVIDENCE}
"""

GOAL_UPDATES = """
    SELECT a.id, a.created AS date, 1 AS rank, a.detail AS text, '[]' AS sources,
           g.title||' · '||a.title AS title, a.goal_id
    FROM goal_activity a JOIN goals g ON g.id=a.goal_id
    WHERE a.artifact=1 AND a.created<=:date
"""

GOAL_NOTICES = """
    SELECT 'notice-'||n.follow_up_id AS id,n.created AS date,1 AS rank,n.text,'[]' AS sources,
           g.title||' · Follow-up scheduled' AS title,f.goal_id
    FROM goal_follow_up_notices n JOIN goal_follow_ups f ON f.id=n.follow_up_id
    JOIN goals g ON g.id=f.goal_id
    WHERE n.chat_request_id IS NULL AND n.text IS NOT NULL AND n.created<=:date AND f.cancelled=0
"""


def bounded(query):
    return f"""SELECT * FROM ({query})
               WHERE (date, rank, id) < (:date, :rank, :id)
               ORDER BY date DESC, rank DESC, id DESC LIMIT :limit"""


# Bound the indexed streams before merging. The final sort handles at most
# five pages of candidates even when the archive contains years of messages.
PAGE_QUERY = "\nUNION ALL\n".join(
    f"SELECT * FROM ({bounded(query)})" for query in (QUESTIONS, ANSWERS, LEGACY_VOICE, GOAL_UPDATES, GOAL_NOTICES)
) + "\nORDER BY date DESC, rank DESC, id DESC LIMIT :limit"


def page_boundary(before):
    if before is None:
        return "9999", 0, ""
    parts = before.split(",", 2)
    if (len(parts) != 3 or not 1 <= len(parts[0]) <= 64
            or parts[1] not in {"0", "1"} or not 1 <= len(parts[2]) <= 128):
        raise ValueError("Invalid chat history cursor.")
    return parts[0], int(parts[1]), parts[2]


def chat_page(store, before=None):
    date, rank, identifier = page_boundary(before)
    with store.connect() as db:
        db.execute("BEGIN")
        rows = db.execute(PAGE_QUERY, {"date": date, "rank": rank, "id": identifier, "limit": PAGE_SIZE + 1}).fetchall()
        follow_ups = for_messages(db, [row["id"] for row in rows[:PAGE_SIZE] if row["goal_id"] is not None])
    page = rows[:PAGE_SIZE]
    next_before = None
    if len(rows) > PAGE_SIZE:
        last = page[-1]
        next_before = f"{last['date']},{last['rank']},{last['id']}"
    return {"items": [chat_message(row, follow_ups.get(row["id"])) for row in reversed(page)],
            "next_before": next_before}


def chat_message(row, follow_up=None):
    identifier, goal = row["id"], row["goal_id"]
    role = "update" if goal is not None else "user" if row["rank"] == 0 else "assistant"
    message = {"id": identifier, "role": role, "text": row["text"], "date": row["date"],
               "sources": [source for source in json.loads(row["sources"]) if source != identifier[2:]]}
    if goal is not None:
        message.update(title=row["title"], goal_id=goal)
    if follow_up:
        message["follow_up"] = follow_up
    return message


def paged_chat_snapshot(store, cursor):
    page = chat_page(store)
    version = hashlib.sha256(json.dumps(page, sort_keys=True).encode()).hexdigest()
    if cursor == version:
        return {"cursor": version}
    return page | {"cursor": version}
