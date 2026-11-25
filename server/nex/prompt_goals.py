"""Persist prompt goals and commit a run only if its definition is current."""

from datetime import timedelta

from .feed import stamp
from .goal_definition import edited_title, field, summary
from .goal_follow_ups import RESERVED_RUN
from .goal_schedule import following_run, parse_schedule
from .pdf import artifacts as pdf_artifacts
from .push_queue import enqueue
from .recall_dates import time_zone
from .workspace_features import revision as feature_revision

GOAL_UPDATE_TITLE = "Goal update"


def save_prompt(db, goals, identifier, data, old, anchor):
    prompt = field(data, "prompt", 1200)
    if not prompt:
        raise ValueError("Describe what you want Nex to do.")
    zone = time_zone(data.get("zone")).key
    previous_start, previous_interval = (old["scheduled_at"], old["interval_minutes"]) if old else (None, 0)
    start, interval = parse_schedule(data, anchor, previous_start if not previous_interval else None)
    previous_prompt = old["prompt"] if old else None
    previous_watch = old["watch_provider"] if old else None
    watch = data.get("watch_provider", previous_watch if prompt == previous_prompt and interval else None)
    if watch is not None and (watch != "gmail" or not interval):
        raise ValueError("Choose a supported repeating watch.")
    title = edited_title(data, prompt, old)
    if old and (prompt, start, interval, zone, watch) == (
        previous_prompt, previous_start, previous_interval, old["zone"], previous_watch,
    ):
        if title == old["title"]:
            return summary(old)
        goals.check_revision(old, data.get("revision"))
        now = stamp(anchor)
        db.execute("UPDATE goals SET title=?,updated=?,revision=revision+1 WHERE id=?", (title, now, identifier))
        goals.record(db, identifier, now, "Goal renamed", title)
        return summary(goals.find(db, identifier))
    goals.check_revision(old, data.get("revision"))
    now = stamp(anchor)
    fields = (title, prompt, zone, interval, start, now, watch)
    if old:
        save_edit(db, goals, old, fields, prompt != previous_prompt, anchor)
    else:
        goals.require_space(db)
        first_run = following_run(start, interval, zone, anchor) if interval and start < now else start
        db.execute(
            "INSERT INTO goals(id,title,prompt,zone,interval_minutes,scheduled_at,updated,watch_provider,status,created,next_run) "
            "VALUES(?,?,?,?,?,?,?,?,'active',?,?)", (identifier, *fields, now, first_run),
        )
        goals.record(db, identifier, now, "Goal created", "Nex saved your prompt and schedule.")
    return summary(goals.find(db, identifier))


def save_edit(db, goals, old, fields, prompt_changed, anchor):
    _, _, zone, interval, start, now, watch = fields
    identifier = old["id"]
    schedule_changed = (start, interval, zone) != (old["scheduled_at"], old["interval_minutes"], old["zone"])
    next_run = old["next_run"]
    if schedule_changed:
        next_run = start if not interval else following_run(start, interval, zone, anchor)
    elif old["requested"]:
        next_run = following_run(start, interval, zone, anchor)
        if not interval and start and (old["last_checked"] is None or old["last_checked"] < start):
            next_run = start
    if old["status"] != "active":
        next_run = None
    if prompt_changed or watch != old["watch_provider"]:
        db.execute("DELETE FROM goal_mail_review WHERE goal_id=?", (identifier,))
    checked = None if schedule_changed else old["last_checked"]
    db.execute(
        "UPDATE goals SET title=?,prompt=?,zone=?,interval_minutes=?,scheduled_at=?,updated=?,watch_provider=?,"
        "next_run=?,last_checked=?,"
        "latest=?,revision=revision+1,error=NULL,requested=0 WHERE id=?",
        (*fields, next_run, checked, None if prompt_changed else old["latest"], identifier),
    )
    goals.record(db, identifier, now, "Goal edited", "Nex saved your prompt and schedule.")


def finish_prompt(goals, row, result, anchor, epoch):
    now, identifier = stamp(anchor), row["id"]
    with goals.store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        if feature_revision(db, "goals") != epoch:
            return
        current = goals.find(db, identifier, required=False)
        if current is None or (current["revision"], current["next_run"]) != (row["revision"], row["next_run"]):
            return
        if db.execute(f"SELECT 1 FROM goals g WHERE g.id=? AND EXISTS ({RESERVED_RUN})", (identifier,)).fetchone():
            return
        next_run = following_run(row["scheduled_at"], row["interval_minutes"], row["zone"], anchor)
        latest, checked = current["latest"], current["last_checked"]
        if result.error:
            next_run = stamp(anchor + timedelta(minutes=5))
            if result.error != current["error"]:
                goals.record(db, identifier, now, "Run needs attention", result.error)
        else:
            if result.text:
                latest = result.text
                activity = goals.record(db, identifier, now, GOAL_UPDATE_TITLE, latest, artifact=True)
                if result.pdf:
                    pdf_artifacts.insert(db, activity, identifier, now, result.pdf)
                if result.pdf_copy:
                    db.execute("INSERT INTO goal_update_pdfs VALUES(?,?,?,?,?,?)",
                               (activity, identifier, result.pdf_copy, current["title"], current["reply_language"], now))
                enqueue(db, activity, anchor)
            if any(watch.pending for watch in result.watches.values()):
                next_run = stamp(anchor + timedelta(seconds=15))
            else:
                checked = now
                if not result.text and (row["requested"] or current["last_checked"] is None):
                    goals.record(db, identifier, now, "Run complete", "No update was needed.")
            for watch in result.watches.values():
                save_watch_review(db, identifier, watch, anchor)
        db.execute("UPDATE goals SET next_run=?,last_checked=?,error=?,latest=?,requested=0 WHERE id=?",
                   (next_run, checked, result.error, latest, identifier))


def save_watch_review(db, identifier, watch, anchor):
    now = stamp(anchor)
    db.executemany("INSERT OR IGNORE INTO goal_seen VALUES(?,?,?)",
                   [(identifier, fingerprint, now) for fingerprint in watch.matches])
    db.executemany("INSERT OR IGNORE INTO goal_mail_review VALUES(?,?,?)",
                   [(identifier, fingerprint, now) for fingerprint in watch.reviewed])
    if watch.reviewed and not watch.pending:
        db.execute("DELETE FROM goal_mail_review WHERE goal_id=? AND reviewed<?",
                   (identifier, stamp(anchor - timedelta(days=2))))
