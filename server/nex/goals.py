"""Manage scheduled and on-demand AI goals."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from . import goal_follow_ups
from .feed import stamp
from .goal_definition import field, summary
from .goal_runner import run_prompt
from .goal_schedule import schedule_after_resume
from .prompt_goals import finish_prompt, save_prompt
from .store import excerpt
from .workspace_features import require
from .workspace_features import revision as feature_revision
from .workspace_schema import WorkspaceConflict, WorkspaceMissing

IDENTIFIER = r"[a-f0-9]{8}-(?:[a-f0-9]{4}-){3}[a-f0-9]{12}"


class Goals:
    def __init__(self, store, classify=None, *, generate=None):
        self.store = store
        self.classify = classify
        self.generate = generate

    def overview(self):
        with self.store.connect() as db:
            rows = db.execute("SELECT * FROM goals ORDER BY updated DESC, id").fetchall()
        return [summary(row) for row in rows]

    def alerts(self, after):
        # Locked clients receive opaque identifiers only.
        cutoff = datetime.now(UTC) - (timedelta(hours=12) if after else timedelta(minutes=15))
        with self.store.connect() as db:
            rows = db.execute(
                "SELECT sequence,id FROM goal_activity WHERE artifact=1 AND sequence>? AND created>=? "
                "ORDER BY sequence LIMIT 100", (after, stamp(cutoff)),
            ).fetchall()
        return {"items": [dict(row) for row in rows]}

    def detail(self, identifier):
        with self.store.connect() as db:
            db.execute("BEGIN")
            row = self.find(db, identifier)
            activity = db.execute(
                "SELECT id,created,title,detail,artifact FROM goal_activity WHERE goal_id=? "
                "ORDER BY created DESC,rowid DESC LIMIT 100", (identifier,),
            ).fetchall()
            count = db.execute(
                "SELECT count(*) FROM (SELECT id FROM goal_activity WHERE goal_id=? AND artifact=1 "
                "UNION SELECT id FROM pdf_artifacts WHERE goal_id=?)", (identifier, identifier),
            ).fetchone()[0]
            follow_up = goal_follow_ups.for_goal(db, identifier)
        return summary(row) | {
            "activity": [dict(item) | {"artifact": bool(item["artifact"])} for item in activity],
            "artifact_count": count,
            "follow_up": follow_up,
        }

    @staticmethod
    def find(db, identifier, *, required=True):
        row = db.execute("SELECT * FROM goals WHERE id=?", (identifier,)).fetchone()
        if required and row is None:
            raise WorkspaceMissing("That goal is unavailable.")
        return row

    @staticmethod
    def record(db, identifier, created, title, detail, *, artifact=False, activity=None):
        activity = activity or str(uuid4())
        db.execute("INSERT INTO goal_activity(id,goal_id,created,title,detail,artifact) VALUES(?,?,?,?,?,?)",
                   (activity, identifier, created, title, detail, int(artifact)))
        return activity

    @staticmethod
    def check_revision(old, version):
        if old and old["revision"] != version:
            raise WorkspaceConflict("This goal changed on another device. Reopen it before saving.")
        if not old and version is not None:
            raise WorkspaceMissing("That goal is unavailable.")

    @staticmethod
    def require_space(db):
        if db.execute("SELECT count(*) FROM goals").fetchone()[0] >= 500:
            raise ValueError("Nex’s goal list has reached its limit.")

    def save(self, identifier, data, anchor=None, *, create_once=False, request="", language=None, check_in=None,
             chat_request_id=None):
        anchor = anchor or datetime.now(UTC)
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            require(db, "goals")
            old = self.find(db, identifier, required=False)
            if old and create_once:
                return summary(old)
            if old is None and self.was_deleted(db, identifier):
                raise WorkspaceMissing("That goal is unavailable.")
            result = save_prompt(db, self, identifier, data, old, anchor)
            result = self.save_assessment(db, old, result, anchor, data.get("revision"), request, language, check_in,
                                          chat_request_id)
            if old is None or old["revision"] != result["revision"]:
                db.execute(
                    "UPDATE goals SET original_request=CASE WHEN original_request='' THEN ? ELSE original_request END,"
                    "reply_language=COALESCE(?, reply_language) WHERE id=?",
                    (request, language if language in ("en", "tr") else None, identifier),
                )
            return result

    def set_pdf_format(self, identifier, revision, instruction):
        if instruction is not None:
            instruction = field({"pdf_instruction": instruction}, "pdf_instruction", 420)
            if not instruction:
                raise ValueError("Specify the PDF output format.")
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            require(db, "goals")
            goal = self.find(db, identifier)
            self.check_revision(goal, revision)
            if instruction is None:
                db.execute("DELETE FROM goal_update_pdfs WHERE goal_id=?", (identifier,))
                db.execute("UPDATE goal_follow_ups SET pdf_instruction=NULL, pdf_retry_at=NULL "
                           "WHERE goal_id=? AND pdf_retry_at IS NOT NULL", (identifier,))
            if instruction != goal["pdf_instruction"]:
                now = stamp(datetime.now(UTC))
                db.execute("UPDATE goals SET pdf_instruction=?,updated=?,revision=revision+1 WHERE id=?",
                           (instruction, now, identifier))
                title = "Goal PDF disabled" if instruction is None else "Goal format updated"
                self.record(db, identifier, now, title, instruction or "")
                goal = self.find(db, identifier)
            return summary(goal)

    def save_assessment(self, db, old, goal, anchor, revision, request, language, check_in, chat_request_id):
        identifier = goal["id"]
        if goal.get("watch_provider"):
            goal_follow_ups.cancel(db, identifier)
            return goal
        previous = goal_follow_ups.assessment(db, identifier) if old else None
        was_required = bool(previous and previous["required"])
        required = (was_required if check_in is None else check_in) and not goal["frequency_minutes"]
        if (old and old["title"] != goal["title"] and required == was_required and
                (old["prompt"], old["scheduled_at"], old["interval_minutes"], old["zone"]) ==
                (goal["prompt"], goal["scheduled_at"], goal["frequency_minutes"], goal["zone"])):
            return goal
        if old and old["revision"] == goal["revision"]:
            if required == was_required:
                return goal
            self.check_revision(old, revision)
            now = stamp(anchor)
            db.execute("UPDATE goals SET revision=revision+1,updated=? WHERE id=?", (now, identifier))
            self.record(db, identifier, now, "Goal edited", "Nex saved your check-in settings.")
            goal = summary(self.find(db, identifier))
        if previous and old["prompt"] == goal["prompt"]:
            request = request or previous["request"]
            language = language or previous["language"]
        goal_follow_ups.assess(db, identifier, anchor, request=excerpt(request, 900),
                               language=language, required=required, chat_request_id=chat_request_id if required else None)
        return goal

    @staticmethod
    def was_deleted(db, identifier):
        return db.execute("SELECT 1 FROM deleted_goal_ids WHERE id=?", (identifier,)).fetchone() is not None

    def delete(self, identifier, version):
        if type(version) is not int or version < 1:
            raise ValueError("Use the current goal revision to delete it.")
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            require(db, "goals")
            if not self.was_deleted(db, identifier):
                self.check_revision(self.find(db, identifier), version)
                db.execute("INSERT INTO deleted_goal_ids VALUES(?)", (identifier,))
                db.execute(
                    "DELETE FROM goal_push_delivery WHERE id IN (SELECT id FROM goal_activity WHERE goal_id=?)",
                    (identifier,),
                )
                db.execute("DELETE FROM goal_activity WHERE goal_id=?", (identifier,))
                db.execute("DELETE FROM goal_update_pdfs WHERE goal_id=?", (identifier,))
                db.execute("DELETE FROM pdf_artifacts WHERE goal_id=?", (identifier,))
                db.execute("DELETE FROM goal_seen WHERE goal_id=?", (identifier,))
                db.execute("DELETE FROM goal_mail_review WHERE goal_id=?", (identifier,))
                goal_follow_ups.delete(db, identifier)
                db.execute("DELETE FROM goals WHERE id=?", (identifier,))
        return {"deleted": True}

    def action(self, identifier, action, version, anchor=None):
        anchor = anchor or datetime.now(UTC)
        now = stamp(anchor)
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            require(db, "goals")
            row = self.find(db, identifier)
            if version != row["revision"]:
                raise WorkspaceConflict("This goal changed on another device. Refresh and try again.")
            if action == "check" and row["status"] == "active":
                db.execute("UPDATE goals SET next_run=?,requested=1,error=NULL,revision=revision+1 WHERE id=?",
                           (now, identifier))
            elif action in ("active", "paused", "completed"):
                if row["status"] == action:
                    return summary(row)
                schedule = schedule_after_resume(row, anchor) if action == "active" else None
                db.execute("UPDATE goals SET status=?,updated=?,next_run=?,error=NULL,revision=revision+1,"
                           "requested=0 WHERE id=?", (action, now, schedule, identifier))
                if action == "completed":
                    goal_follow_ups.cancel(db, identifier)
                self.record(db, identifier, now, {"active": "Goal resumed", "paused": "Goal paused",
                                                 "completed": "Goal completed"}[action], "")
            else:
                raise ValueError("This action is unavailable for the goal.")
            return summary(self.find(db, identifier))

    def run(self, anchor=None):
        anchor = anchor or datetime.now(UTC)
        with self.store.connect() as db:
            epoch = feature_revision(db, "goals")
            if epoch is None:
                return
            due = db.execute(
                "SELECT g.* FROM goals g WHERE status='active' AND next_run<=? "
                f"AND NOT EXISTS ({goal_follow_ups.RESERVED_RUN}) ORDER BY next_run LIMIT 12", (stamp(anchor),),
            ).fetchall()
        for row in due:
            with self.store.connect() as db:
                if feature_revision(db, "goals") != epoch:
                    return
                current = self.find(db, row["id"], required=False)
                if current is None or (current["revision"], current["next_run"]) != (row["revision"], row["next_run"]):
                    continue
            result = run_prompt(self.store, row, anchor, self.generate, self.classify)
            finish_prompt(self, row, result, anchor, epoch)
