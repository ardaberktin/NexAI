"""Persist the plan and dispatch receipts for a goal occurrence that can write."""

import json
from dataclasses import dataclass

from ..workspace_features import revision as feature_revision
from . import MCPError

SCHEMA = """
CREATE TABLE IF NOT EXISTS mcp_goal_runs (
    goal_id TEXT PRIMARY KEY, due TEXT NOT NULL, revision INTEGER NOT NULL,
    plan TEXT NOT NULL, receipts TEXT NOT NULL
);
"""


@dataclass(frozen=True)
class GoalWrites:
    store: object
    goal_id: str
    due: str
    revision: int
    epoch: int

    def entry(self, db):
        return db.execute("SELECT * FROM mcp_goal_runs WHERE goal_id=? AND due=?", (self.goal_id, self.due)).fetchone()

    def current(self, db):
        goal = db.execute("SELECT status,revision,next_run FROM goals WHERE id=?", (self.goal_id,)).fetchone()
        if (feature_revision(db, "goals") != self.epoch or goal is None
                or (goal["status"], goal["revision"], goal["next_run"]) != ("active", self.revision, self.due)):
            raise MCPError("This goal changed before the MCP write could run.")

    def plan(self):
        with self.store.connect() as db:
            entry = self.entry(db)
            if entry is None:
                return None
            if entry["revision"] != self.revision:
                raise MCPError("This goal changed after its MCP write run was prepared.")
            return json.loads(entry["plan"])

    def prepare(self, tools):
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self.current(db)
            entry = self.entry(db)
            if entry is not None:
                if entry["revision"] != self.revision:
                    raise MCPError("This goal changed after its MCP write run was prepared.")
                return json.loads(entry["plan"])
            db.execute(
                "INSERT INTO mcp_goal_runs VALUES(?,?,?,?,'{}') ON CONFLICT(goal_id) DO UPDATE SET "
                "due=excluded.due,revision=excluded.revision,plan=excluded.plan,receipts=excluded.receipts",
                (self.goal_id, self.due, self.revision, json.dumps(tools)),
            )
        return tools

    def receipts(self):
        with self.store.connect() as db:
            entry = self.entry(db)
            return json.loads(entry["receipts"]) if entry else {}

    def receipt(self, alias):
        return self.receipts().get(alias)

    def dispatch(self, call, server, tool, operation, authorize, invoke):
        alias = call["name"]
        pending = {"status": "unconfirmed", "server": server, "tool": tool, "operation": operation,
                   "detail": "The MCP server did not confirm the operation. Review the connected service."}
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self.current(db)
            authorize(db)
            entry = self.entry(db)
            if entry is None or entry["revision"] != self.revision or call not in json.loads(entry["plan"]):
                raise MCPError("A saved run is required for this MCP write.")
            receipts = json.loads(entry["receipts"])
            if alias in receipts:
                return receipts[alias]
            receipts[alias] = pending
            self.save_receipts(db, receipts)
        # The reservation is durable before dispatch. An interrupted call is never replayed automatically.
        try:
            result = invoke()
        except MCPError:
            return pending
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if entry := self.entry(db):
                receipts = json.loads(entry["receipts"])
                receipts[alias] = bounded_receipt(result)
                self.save_receipts(db, receipts)
        return result

    def save_receipts(self, db, receipts):
        db.execute("UPDATE mcp_goal_runs SET receipts=? WHERE goal_id=? AND due=?",
                   (json.dumps(receipts), self.goal_id, self.due))


def bounded_receipt(outcome):
    if len(json.dumps(outcome.get("structured_content"))) <= 16_000:
        return outcome
    return {key: value for key, value in outcome.items() if key != "structured_content"} | {
        "structured_content_omitted": True,
    }
