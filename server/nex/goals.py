"""Manage scheduled and on-demand AI goals."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from .feed import stamp
from .goal_definition import summary
from .goal_runner import run_prompt
from .goal_schedule import schedule_after_resume
from .prompt_goals import finish_prompt, save_prompt
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
                "SELECT rowid AS sequence,id FROM goal_activity WHERE artifact=1 AND rowid>? AND created>=? "
                "ORDER BY rowid LIMIT 100", (after, stamp(cutoff)),
            ).fetchall()
        return {"items": [dict(row) for row in rows]}

    def detail(self, identifier):
        with self.store.connect() as db:
            row = self.find(db, identifier)
            activity = db.execute(
                "SELECT id,created,title,detail,artifact FROM goal_activity WHERE goal_id=? "
                "ORDER BY created DESC,rowid DESC LIMIT 100", (identifier,),
            ).fetchall()
            count = db.execute("SELECT count(*) FROM goal_activity WHERE goal_id=? AND artifact=1",
                               (identifier,)).fetchone()[0]
        return summary(row) | {
            "activity": [dict(item) | {"artifact": bool(item["artifact"])} for item in activity],
            "artifact_count": count,
        }

    @staticmethod
    def find(db, identifier):
        row = db.execute("SELECT * FROM goals WHERE id=?", (identifier,)).fetchone()
        if row is None:
            raise WorkspaceMissing("That goal is unavailable.")
        return row

    @staticmethod
    def record(db, identifier, created, title, detail, *, artifact=False):
        activity = str(uuid4())
        db.execute("INSERT INTO goal_activity VALUES(?,?,?,?,?,?)",
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

    def save(self, identifier, data, anchor=None, *, create_once=False):
        anchor = anchor or datetime.now(UTC)
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            require(db, "goals")
            old = db.execute("SELECT * FROM goals WHERE id=?", (identifier,)).fetchone()
            if old and create_once:
                return summary(old)
            return save_prompt(db, self, identifier, data, old, anchor)

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
            due = db.execute("SELECT * FROM goals WHERE status='active' AND next_run<=? "
                             "ORDER BY next_run LIMIT 12", (stamp(anchor),)).fetchall()
        for row in due:
            with self.store.connect() as db:
                if feature_revision(db, "goals") != epoch:
                    return
                current = self.find(db, row["id"])
                if (current["revision"], current["next_run"]) != (row["revision"], row["next_run"]):
                    continue
            result = run_prompt(self.store, row, anchor, self.generate, self.classify)
            finish_prompt(self, row, result, anchor, epoch)
