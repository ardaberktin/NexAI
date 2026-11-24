"""Ask Nex to choose follow-up actions and deliver due questions."""

import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from .accounts.credentials import Credentials
from .feed import stamp
from .follow_up_timing import choose_time
from .follow_up_tools import Schedule, choose_follow_up
from .goals import Goals
from .push_queue import enqueue
from .recall_dates import time_zone
from .store import excerpt
from .workspace_features import revision as feature_revision

DECISIONS = """
    SELECT g.*, w.request, w.language, w.answered_id, w.next_try, w.required, w.chat_request_id
    FROM goal_follow_up_work w JOIN goals g ON g.id=w.goal_id
    WHERE g.status='active' AND w.next_try<=?
"""


def available_run(goal):
    if (not goal["answered_id"] and not goal["interval_minutes"] and not goal["requested"]
            and goal["next_run"] == goal["scheduled_at"]):
        return goal["next_run"]
    return None


def local_time(value, zone):
    return datetime.fromisoformat(value).astimezone(time_zone(zone)).isoformat() if value else None


class GoalFollowUpJobs:
    def __init__(self, store, generate):
        self.store = store
        self.generate = generate

    def decide(self, anchor=None):
        supplied_anchor = anchor
        anchor = anchor or datetime.now(UTC)
        now = stamp(anchor)
        with self.store.connect() as db:
            epoch = feature_revision(db, "goals")
            if epoch is None:
                return
            work = db.execute(DECISIONS + " ORDER BY w.next_try,g.id LIMIT 6", (now,)).fetchall()
        for job in work:
            with self.store.connect() as db:
                if feature_revision(db, "goals") != epoch:
                    return
                answers = self.answers(db, job)
            data = self.context(job, answers, now)
            decision = choose_follow_up(self.generate, data, available_run(job))
            if isinstance(decision, Schedule) and not decision.uses_goal_run:
                decision = choose_time(self.generate, decision, data, anchor)
            self.apply(job, decision, supplied_anchor or datetime.now(UTC), epoch)

    @staticmethod
    def answers(db, job):
        if not job["answered_id"]:
            return []
        rows = db.execute(
            "SELECT question,choices,selected_choice,answered_at FROM goal_follow_ups "
            "WHERE goal_id=? AND selected_choice IS NOT NULL AND cancelled=0 ORDER BY rowid DESC LIMIT 3",
            (job["id"],),
        ).fetchall()
        return [
            {"question": excerpt(row["question"], 250), "selected": json.loads(row["choices"])[row["selected_choice"]],
             "answered_at": local_time(row["answered_at"], job["zone"])} for row in reversed(rows)
        ]

    def context(self, goal, answers, now):
        zone = goal["zone"]
        return {"goal": goal["prompt"], "original_request": goal["request"], "language": goal["language"],
                "local_time": local_time(now, zone), "time_zone": zone,
                "first_run": local_time(goal["scheduled_at"], zone),
                "next_run": local_time(goal["next_run"], zone), "frequency_minutes": goal["interval_minutes"],
                "available_check_in_run": local_time(available_run(goal), zone),
                "check_in_requested": bool(goal["required"]),
                "account_access": Credentials(self.store.root).status(),
                "earlier_answers": answers[:-1], "latest_answer": answers[-1] if answers else None}

    def apply(self, job, decision, anchor, epoch):
        now, identifier = stamp(anchor), job["id"]
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if feature_revision(db, "goals") != epoch:
                return
            current = db.execute(DECISIONS + " AND g.id=?", (now, identifier)).fetchone()
            if current is None or any(
                current[key] != job[key] for key in ("revision", "answered_id", "next_try", "required")
            ):
                return
            if isinstance(decision, Schedule) and decision.uses_goal_run and available_run(current) != stamp(decision.due):
                return
            if isinstance(decision, Schedule) and not decision.uses_goal_run and decision.due <= anchor:
                decision = None
            if isinstance(decision, Schedule):
                follow_up_id = str(uuid4())
                db.execute(
                    "INSERT INTO goal_follow_ups(id,goal_id,due,question,choices,uses_goal_run) VALUES(?,?,?,?,?,?)",
                    (follow_up_id, identifier, stamp(decision.due), decision.question,
                     json.dumps(decision.choices, ensure_ascii=False), decision.uses_goal_run),
                )
                db.execute("INSERT INTO goal_follow_up_notices(follow_up_id,next_try,chat_request_id) VALUES(?,?,?)",
                           (follow_up_id, now, current["chat_request_id"]))
            if decision == "complete_goal":
                db.execute("UPDATE goals SET status='completed',updated=?,next_run=NULL,error=NULL,"
                           "revision=revision+1,requested=0 WHERE id=?", (now, identifier))
                Goals.record(db, identifier, now, "Goal completed", "")
            # Retry infrastructure failures without inventing a question or cadence.
            retry = stamp(anchor + timedelta(minutes=1)) if decision is None else None
            db.execute("UPDATE goal_follow_up_work SET next_try=? WHERE goal_id=?", (retry, identifier))

    def deliver(self, anchor=None):
        anchor = anchor or datetime.now(UTC)
        now = stamp(anchor)
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if feature_revision(db, "goals") is None:
                return
            due = db.execute(
                "SELECT f.id,f.goal_id,f.due,f.question,f.uses_goal_run,g.pdf_instruction "
                "FROM goal_follow_ups f JOIN goals g ON g.id=f.goal_id "
                "WHERE f.published_at IS NULL AND f.cancelled=0 AND f.due<=? AND g.status='active' "
                "ORDER BY f.due,f.id LIMIT 24", (now,),
            ).fetchall()
            for row in due:
                instruction = row["pdf_instruction"]
                db.execute("UPDATE goal_follow_ups SET published_at=?,pdf_instruction=?,pdf_retry_at=? WHERE id=?",
                           (now, instruction, now if instruction else None, row["id"]))
                db.execute("DELETE FROM goal_follow_up_notices WHERE follow_up_id=? AND text IS NULL", (row["id"],))
                Goals.record(db, row["goal_id"], now, "Follow-up", row["question"], artifact=True, activity=row["id"])
                if row["uses_goal_run"]:
                    db.execute("UPDATE goals SET next_run=NULL,last_checked=?,error=NULL "
                               "WHERE id=? AND next_run=? AND requested=0", (now, row["goal_id"], row["due"]))
                enqueue(db, row["id"], anchor)
