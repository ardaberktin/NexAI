"""Store a goal's follow-ups and the user's recorded answers."""

import json
from datetime import UTC, datetime

from .feed import stamp
from .workspace_features import enabled, require
from .workspace_schema import WorkspaceConflict, WorkspaceMissing

RESERVED_RUN = """
    SELECT 1 FROM goal_follow_ups f
    WHERE f.goal_id=g.id AND f.uses_goal_run=1 AND f.due=g.next_run
      AND f.published_at IS NULL AND f.cancelled=0 AND g.requested=0
    UNION ALL
    SELECT 1 FROM goal_follow_up_work w
    WHERE w.goal_id=g.id AND w.required=1 AND w.answered_id IS NULL
      AND w.next_try IS NOT NULL AND g.requested=0
"""

READ_CARD = """
    SELECT f.*, g.status FROM goal_follow_ups f JOIN goals g ON g.id=f.goal_id
"""


def assessment(db, goal_id):
    return db.execute("SELECT request,language,required FROM goal_follow_up_work WHERE goal_id=?",
                      (goal_id,)).fetchone()


def assess(db, goal_id, anchor, *, request="", language=None, required=False, chat_request_id=None):
    discard_pending_alerts(db, goal_id)
    db.execute("UPDATE goal_follow_ups SET cancelled=1 WHERE goal_id=? AND cancelled=0", (goal_id,))
    now = stamp(anchor)
    db.execute(
        "INSERT INTO goal_follow_up_work(goal_id,request,language,answered_id,next_try,required,chat_request_id) "
        "VALUES(?,?,?,NULL,?,?,?) "
        "ON CONFLICT(goal_id) DO UPDATE SET request=excluded.request,language=excluded.language,"
        "answered_id=NULL,next_try=excluded.next_try,required=excluded.required,chat_request_id=excluded.chat_request_id",
        (goal_id, request, language, now, required, chat_request_id),
    )


def cancel(db, goal_id):
    discard_pending_alerts(db, goal_id)
    db.execute("UPDATE goal_follow_ups SET cancelled=1 WHERE goal_id=? AND selected_choice IS NULL", (goal_id,))
    db.execute("UPDATE goal_follow_up_work SET next_try=NULL WHERE goal_id=?", (goal_id,))


def discard_pending_alerts(db, goal_id):
    db.execute("DELETE FROM goal_push_delivery WHERE id IN "
               "(SELECT id FROM goal_follow_ups WHERE goal_id=? AND selected_choice IS NULL)", (goal_id,))


def delete(db, goal_id):
    db.execute("DELETE FROM goal_follow_up_notices WHERE follow_up_id IN "
               "(SELECT id FROM goal_follow_ups WHERE goal_id=?)", (goal_id,))
    db.execute("DELETE FROM goal_follow_ups WHERE goal_id=?", (goal_id,))
    db.execute("DELETE FROM goal_follow_up_work WHERE goal_id=?", (goal_id,))


def card(row, *, goals_enabled=True):
    published = row["published_at"] is not None
    return {
        "id": row["id"], "due": row["due"], "question": row["question"] if published else None,
        "choices": json.loads(row["choices"]) if published else [],
        "selected_choice": row["selected_choice"],
        "can_answer": (published and row["selected_choice"] is None and not row["cancelled"]
                       and row["status"] == "active" and goals_enabled),
    }


def for_goal(db, goal_id):
    row = db.execute(
        READ_CARD + " WHERE f.goal_id=? AND f.cancelled=0 ORDER BY f.rowid DESC LIMIT 1", (goal_id,),
    ).fetchone()
    return card(row) if row is not None else None


def for_messages(db, ids):
    if not ids:
        return {}
    placeholders = ",".join("?" for _ in ids)
    rows = db.execute(READ_CARD + f" WHERE f.id IN ({placeholders})", ids).fetchall()
    goals_enabled = enabled(db, "goals") if rows else False
    return {row["id"]: card(row, goals_enabled=goals_enabled) for row in rows}


def answer(store, goal_id, identifier, selected, anchor=None):
    if type(selected) is not int:
        raise ValueError("Choose one of the follow-up responses.")
    now = stamp(anchor or datetime.now(UTC))
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        require(db, "goals")
        row = db.execute(READ_CARD + " WHERE f.id=? AND f.goal_id=?", (identifier, goal_id)).fetchone()
        if row is None:
            raise WorkspaceMissing("That follow-up is unavailable.")
        response = card(row)
        if row["published_at"] is None or (row["selected_choice"] is None and not response["can_answer"]):
            raise WorkspaceConflict("This follow-up is no longer available to answer.")
        if not 0 <= selected < len(response["choices"]):
            raise ValueError("Choose one of the follow-up responses.")
        if row["selected_choice"] is not None:
            return response
        db.execute("UPDATE goal_follow_ups SET selected_choice=?,answered_at=? WHERE id=?",
                   (selected, now, identifier))
        db.execute("UPDATE goal_follow_up_work SET answered_id=?,next_try=?,chat_request_id=NULL WHERE goal_id=?",
                   (identifier, now, goal_id))
        return response | {"selected_choice": selected, "can_answer": False}
