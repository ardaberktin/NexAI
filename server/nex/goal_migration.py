"""Translate old goal records to prompts during the database upgrade."""

from datetime import datetime

from .feed import stamp
from .goal_schedule import next_local_run
from .workspace_schema import GOALS_INDEX, GOALS_TABLE


def legacy_definition(goal):
    if goal.get("prompt") is not None:
        return goal["prompt"], goal.get("scheduled_at"), goal["interval_minutes"]

    kind, instruction, topic = goal["kind"], goal["instructions"].strip(), goal["topic"]
    subject = instruction or goal["title"]
    if kind == "reminder":
        return f"Remind me about this: {subject}", goal["reminder_at"], 0
    if kind == "email":
        prompt = (f'Notify me when a new Gmail message contains the exact phrase "{topic}".' if topic else
                  f"Notify me about new Gmail messages that match this description: {subject}")
        return prompt, goal["created"], goal["interval_minutes"]
    if kind == "calendar":
        match = f" that match {topic}" if topic else ""
        prompt = f"Notify me about newly found Google Calendar events{match} in the next seven days. {instruction}"
        return prompt.strip(), goal["created"], goal["interval_minutes"]
    if kind == "briefing":
        created = datetime.fromisoformat(goal["created"])
        clock = goal.get("briefing_time") or "08:00"
        start = stamp(next_local_run(clock, goal["zone"], created))
        prompt = instruction or "Give me a morning briefing with today’s agenda, local weather and important unread Gmail."
        return prompt, start, 1440
    return subject, None, 0


def migrate_goals(db):
    # The enclosing migration transaction keeps the old table intact if any conversion fails.
    db.execute("ALTER TABLE goals RENAME TO legacy_goals")
    db.execute("DROP INDEX IF EXISTS goals_due")
    db.execute(GOALS_TABLE)
    db.execute(GOALS_INDEX)
    rows = (dict(row) for row in db.execute("SELECT * FROM legacy_goals"))
    for row in rows:
        prompt, start, interval = legacy_definition(row)
        db.execute(
            "INSERT INTO goals(id,title,prompt,zone,interval_minutes,scheduled_at,status,created,updated,revision,"
            "next_run,last_checked,error,latest,requested) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (row["id"], row["title"], prompt[:1200], row["zone"], interval, start, row["status"],
             row["created"], row["updated"], row["revision"], row["next_run"], row["last_checked"], row["error"],
             row["latest"], row["requested"]),
        )
    db.execute("DROP TABLE legacy_goals")
