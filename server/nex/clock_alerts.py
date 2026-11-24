"""Store one-shot alarms and timers and deliver each due alert independently of the model."""

import json
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from .push_queue import enqueue
from .recall_dates import time_key
from .workspace_features import enabled, require
from .workspace_schema import WorkspaceMissing

LOCAL_GRACE = timedelta(seconds=15)
CARDS = """
    SELECT c.*, EXISTS(
        SELECT 1 FROM clock_native_alarms n WHERE n.alarm_id=c.id
        AND NOT EXISTS(SELECT 1 FROM iphone_push_registration r WHERE r.installation<>n.installation)
    ) AS native_installed FROM clock_alerts c
"""


def card(row):
    result = {key: row[key] for key in ("id", "kind", "label", "created_at", "due_at", "zone", "status")}
    # SQLite row membership checks values, so explicitly check for the optional column.
    has_native = "native_installed" in row.keys()  # noqa: SIM118
    if row["status"] == "scheduled" and has_native and row["native_installed"]:
        result["iphone_alarm_scheduled"] = True
    return result


def uuid_string(value):
    try:
        if str(UUID(value)) == value:
            return value
    except (TypeError, ValueError, AttributeError):
        pass
    raise ValueError("Expected a valid identifier.")


class ClockAlerts:
    def __init__(self, store):
        self.store = store

    def create(self, request_id, kind, label, zone, deadline, *, anchor=None):
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            require(db, "goals")
            if row := db.execute("SELECT * FROM clock_alerts WHERE request_id=?", (request_id,)).fetchone():
                return card(row)
            anchor = anchor or datetime.now(UTC)
            due = deadline(anchor)
            if due <= anchor:
                raise ValueError("The requested time has passed.")
            identifier = str(uuid4())
            db.execute(
                "INSERT INTO clock_alerts VALUES(?,?,?,?,?,?,?,'scheduled')",
                (identifier, request_id, kind, label, time_key(anchor), time_key(due), zone),
            )
            return card(self.find(db, identifier))

    @staticmethod
    def find(db, identifier):
        row = db.execute("SELECT * FROM clock_alerts WHERE id=?", (identifier,)).fetchone()
        if row is None:
            raise WorkspaceMissing("This alarm or timer is unavailable.")
        return row

    def overview(self, *, active_only=False, anchor=None):
        now = time_key(anchor or datetime.now(UTC))
        with self.store.connect() as db:
            db.execute("BEGIN")
            require(db, "goals")
            active = db.execute(CARDS + " WHERE status='scheduled' AND due_at>? "
                                "ORDER BY due_at,id", (now,)).fetchall()
            if active_only:
                return [card(row) for row in active]
            recent = db.execute("SELECT * FROM clock_alerts WHERE status!='scheduled' OR due_at<=? "
                                "ORDER BY created_at DESC,id LIMIT 12", (now,)).fetchall()
        return [card(row) for row in (*active, *recent)]

    def for_requests(self, identifiers):
        if not identifiers:
            return {}
        keys = ",".join("?" for _ in identifiers)
        with self.store.connect() as db:
            if not enabled(db, "goals"):
                return {}
            rows = db.execute(CARDS + f" WHERE request_id IN ({keys})", identifiers)
            return {row["request_id"]: card(row) for row in rows}

    def cancel(self, identifier, *, anchor=None):
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            require(db, "goals")
            row = self.find(db, identifier)
            now = time_key(anchor or datetime.now(UTC))
            if row["status"] != "scheduled":
                return card(row)
            if row["due_at"] <= now:
                return card(row) | {"status": "fired"}
            db.execute("UPDATE clock_alerts SET status='cancelled' WHERE id=?", (identifier,))
            db.execute("DELETE FROM clock_native_alarms WHERE alarm_id=?", (identifier,))
            return card(row) | {"status": "cancelled"}

    def notification_schedule(self, device, *, installation=None, anchor=None):
        # The paired device can schedule a sound while the workspace is locked.
        # Labels and request IDs stay behind the workspace session.
        now = anchor or datetime.now(UTC)
        if installation is not None:
            uuid_string(installation)
        with self.store.connect() as db:
            db.execute("BEGIN")
            if not enabled(db, "goals"):
                return {"items": [], "uses_push": False} if installation is not None else {"items": []}
            push = (device == "iphone"
                    and db.execute("SELECT 1 FROM iphone_push_registration").fetchone() is not None)
            if push and installation is None:
                return {"items": []}
            cutoff = time_key(now - LOCAL_GRACE)
            if installation is not None:
                rows = db.execute(
                    "SELECT c.id,c.kind,c.due_at,n.alarm_id IS NOT NULL AS native_installed "
                    "FROM clock_alerts c LEFT JOIN clock_native_alarms n ON n.alarm_id=c.id AND n.installation=? "
                    "WHERE c.status IN ('scheduled','fired') AND c.due_at>? AND (?=0 OR c.kind='alarm') "
                    "ORDER BY c.due_at,c.id", (installation, cutoff, push),
                ).fetchall()
                return {"items": [dict(row) | {"native_installed": bool(row["native_installed"])} for row in rows],
                        "uses_push": push}
            rows = db.execute("SELECT id,kind,due_at FROM clock_alerts "
                              "WHERE status IN ('scheduled','fired') AND due_at>? ORDER BY due_at,id",
                              (cutoff,)).fetchall()
        return {"items": [dict(row) for row in rows]}

    def report_native(self, installation, identifiers):
        uuid_string(installation)
        if not isinstance(identifiers, list):
            raise ValueError("Expected a list of scheduled alarms.")  # noqa: TRY004 - HTTP validation error
        selected = json.dumps([uuid_string(value) for value in identifiers])
        now = time_key(datetime.now(UTC))
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            require(db, "goals")
            db.execute(
                "DELETE FROM clock_native_alarms WHERE installation=? AND alarm_id IN "
                "(SELECT id FROM clock_alerts WHERE status='scheduled' AND due_at>? "
                "AND id NOT IN (SELECT value FROM json_each(?)))", (installation, now, selected),
            )
            db.execute(
                "INSERT INTO clock_native_alarms(alarm_id,installation) "
                "SELECT id,? FROM clock_alerts WHERE kind='alarm' AND status='scheduled' AND due_at>? "
                "AND id IN (SELECT value FROM json_each(?)) "
                "ON CONFLICT(alarm_id) DO UPDATE SET installation=excluded.installation", (installation, now, selected),
            )
        return {"ok": True}

    def run(self, anchor=None):
        anchor = anchor or datetime.now(UTC)
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if not enabled(db, "goals"):
                return
            due = db.execute("UPDATE clock_alerts SET status='fired' WHERE status='scheduled' AND due_at<=? "
                             "RETURNING id,due_at", (time_key(anchor),)).fetchall()
            for row in due:
                if db.execute(
                    "SELECT 1 FROM clock_native_alarms n JOIN iphone_push_registration r ON n.installation=r.installation "
                    "WHERE n.alarm_id=?", (row["id"],),
                ).fetchone():
                    continue
                # A server restart should not ring alarms from earlier in the day.
                enqueue(db, row["id"], anchor, expires=datetime.fromisoformat(row["due_at"]) + LOCAL_GRACE)
