"""Resolve a Chat check-in's private answer from the scheduler's committed confirmation."""

from datetime import UTC, datetime

from .feed import stamp


def resolve_receipts(db, root, items):
    ready = {(item["id"], item["goal_id"]): item for item in items if item["state"] == "ready" and item["goal_id"]}
    if not ready:
        return
    db.execute("ATTACH DATABASE ? AS workspace", ((root / "memory.sqlite").resolve().as_uri() + "?mode=ro",))
    ids = [request for request, _ in ready]
    keys = ",".join("?" for _ in ids)
    rows = db.execute(f"""
        WITH active AS (
            SELECT id FROM workspace.goals WHERE status='active'
            AND NOT EXISTS (SELECT 1 FROM workspace.workspace_features WHERE name='goals' AND enabled=0)
        )
        SELECT w.chat_request_id,w.goal_id,NULL AS text FROM workspace.goal_follow_up_work w
        WHERE w.chat_request_id IN ({keys}) AND w.next_try IS NOT NULL AND w.goal_id IN active
        UNION ALL
        SELECT n.chat_request_id,f.goal_id,n.text FROM workspace.goal_follow_up_notices n
        JOIN workspace.goal_follow_ups f ON f.id=n.follow_up_id
        WHERE n.chat_request_id IN ({keys}) AND (n.text IS NOT NULL OR
              (n.next_try IS NOT NULL AND f.cancelled=0 AND f.published_at IS NULL AND f.goal_id IN active))
    """, [*ids, *ids])
    for request, goal, text in rows:
        item = ready.get((request, goal))
        if item is None:
            continue
        if text is None:
            item.update(state="working", answer=None)
        elif item["answer"] != text:
            # Preserve the delivered receipt in private history even if its goal is deleted later.
            db.execute("UPDATE requests SET answer=? WHERE id=?", (text, request))
            item["answer"] = text


def migrate_chat_receipts(db, root):
    db.execute("ALTER TABLE goal_follow_up_work ADD COLUMN chat_request_id TEXT")
    if "chat_request_id" not in {row["name"] for row in db.execute("PRAGMA table_info(goal_follow_up_notices)")}:
        db.execute("ALTER TABLE goal_follow_up_notices ADD COLUMN chat_request_id TEXT")
    private = root / "privacy.sqlite"
    if not private.is_file():
        return
    db.execute("ATTACH DATABASE ? AS owner", (private.resolve().as_uri() + "?mode=ro",))
    if not db.execute("SELECT 1 FROM owner.sqlite_master WHERE type='table' AND name='request_goals'").fetchone():
        return
    # Old Chat creates used the private request ID as the goal ID. Only relink the original,
    # unedited assessment, before an answer has started a new check-in cycle.
    db.execute("""
        UPDATE goal_follow_up_work SET chat_request_id=goal_id WHERE required=1 AND answered_id IS NULL
        AND goal_id IN (SELECT g.id FROM goals g JOIN owner.request_goals r ON r.goal_id=g.id
                        WHERE r.request_id=g.id AND g.revision=1 AND g.status='active')
    """)
    now = stamp(datetime.now(UTC))
    db.execute("""
        UPDATE goal_follow_up_notices AS n SET chat_request_id=f.goal_id,text=NULL,created=NULL,next_try=?
        FROM goal_follow_ups f JOIN goal_follow_up_work w ON w.goal_id=f.goal_id
        WHERE f.id=n.follow_up_id AND w.chat_request_id=f.goal_id AND f.cancelled=0
              AND f.published_at IS NULL AND f.due>?
    """, (now, now))
