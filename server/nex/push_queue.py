"""Durable, private delivery for iPhone workspace alerts."""

import logging
from datetime import UTC, datetime
from uuid import UUID

from .apns import HOSTS, TOKEN, Provider
from .workspace_features import enabled

logger = logging.getLogger(__name__)


def enqueue(db, identifier, anchor, *, expires=None):
    now = int(anchor.timestamp())
    expiry = int(expires.timestamp()) if expires is not None else now + 12 * 3600
    if expiry <= now:
        return
    db.execute(
        "INSERT OR IGNORE INTO goal_push_delivery(id,token,next_try,expires) "
        "SELECT ?,token,?,? FROM iphone_push_registration WHERE singleton=1",
        (identifier, now, expiry),
    )


class PushQueue:
    def __init__(self, store):
        self.store = store
        self.provider = None
        self.signature = None

    def register(self, installation, token, environment, enabled, *, device_id):
        try:
            if str(UUID(installation)) != installation or type(enabled) is not bool:
                raise ValueError
        except (TypeError, ValueError, AttributeError):
            raise ValueError("Use a valid iPhone push registration.") from None
        if enabled and (not isinstance(token, str) or not TOKEN.fullmatch(token) or environment not in tuple(HOSTS)):
            raise ValueError("Use a valid iPhone push token and environment.")
        provider = Provider.load(self.store.root) if enabled else None
        if enabled and (provider is None or provider.environment != environment):
            return {"active": False}
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if enabled:
                db.execute("INSERT OR REPLACE INTO iphone_push_registration VALUES(1,?,?,?)",
                           (installation, token, environment))
                db.execute("INSERT OR REPLACE INTO iphone_push_session VALUES(1,?)", (device_id,))
                db.execute("DELETE FROM goal_push_delivery WHERE token<>?", (token,))
            else:
                db.execute("DELETE FROM goal_push_delivery WHERE token IN "
                           "(SELECT token FROM iphone_push_registration WHERE installation=?)", (installation,))
                removed = db.execute("DELETE FROM iphone_push_registration WHERE installation=?", (installation,))
                if removed.rowcount:
                    db.execute("DELETE FROM iphone_push_session")
        return {"active": enabled}

    def provider_for_delivery(self):
        try:
            directory = self.store.root / "apns"
            files = [(directory / name).stat() for name in ("config.json", "key.p8")]
            signature = tuple((info.st_ino, info.st_mtime_ns, info.st_mode) for info in files)
        except OSError:
            self.signature = self.provider = None
            return None
        if self.signature != signature:
            self.provider = Provider.load(self.store.root)
            self.signature = signature
        return self.provider

    def run(self, anchor=None, *, provider=None):
        now = int((anchor or datetime.now(UTC)).timestamp())
        if not self.registration_current():
            return
        with self.store.connect() as db:
            if not enabled(db, "goals"):
                return
            if db.execute("SELECT 1 FROM goal_push_delivery WHERE expires<=? LIMIT 1", (now,)).fetchone():
                db.execute("DELETE FROM goal_push_delivery WHERE expires<=?", (now,))
            rows = db.execute(
                "SELECT q.*,r.environment,c.kind AS clock_kind FROM goal_push_delivery q "
                "JOIN iphone_push_registration r ON q.token=r.token "
                "LEFT JOIN clock_alerts c ON c.id=q.id "
                "WHERE next_try<=? ORDER BY c.kind IS NULL,next_try LIMIT 4", (now,),
            ).fetchall()
        if not rows:
            return
        provider = provider or self.provider_for_delivery()
        for row in rows:
            if not self.registration_current():
                return
            with self.store.connect() as db:
                if not enabled(db, "goals"):
                    return
                if db.execute("SELECT 1 FROM goal_push_delivery WHERE id=? AND token=?",
                              (row["id"], row["token"])).fetchone() is None:
                    continue
            if provider is None or provider.environment != row["environment"]:
                self.finish(row, now, 503, "Configuration")
                continue
            options = {"clock_kind": row["clock_kind"]} if row["clock_kind"] else {}
            status, reason = provider.send(row["token"], row["id"], row["expires"], **options)
            self.finish(row, now, status, reason)

    def registration_current(self):
        accounts = getattr(self.store, "hosted_accounts", None)
        if accounts is None:
            return True
        with self.store.connect() as db:
            registration = db.execute(
                """SELECT r.installation,r.token,s.device_id FROM iphone_push_registration r
                   LEFT JOIN iphone_push_session s USING(singleton)"""
            ).fetchone()
            if registration is None:
                return False
            device_id = registration["device_id"]
            current = device_id and accounts.device_active(device_id)
            if current:
                return True
            # Only clear the registration we inspected. A fresh sign-in may already have registered.
            db.execute("BEGIN IMMEDIATE")
            removed = db.execute(
                """DELETE FROM iphone_push_registration WHERE installation=? AND token=?
                   AND (SELECT device_id FROM iphone_push_session WHERE singleton=1) IS ?""", tuple(registration)
            )
            if removed.rowcount:
                db.execute("DELETE FROM iphone_push_session")
                db.execute("DELETE FROM goal_push_delivery WHERE token=?", (registration["token"],))
            return False

    def finish(self, row, now, status, reason):
        invalid = status == 410 or (status == 400 and reason in {"BadDeviceToken", "DeviceTokenNotForTopic"})
        retry = status in {403, 429} or 500 <= status < 600
        if status == 200:
            logger.info("Apple accepted Nex alert %s.", row["id"])
        else:
            logger.warning("Nex iPhone push failed (HTTP %d).", status)
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if invalid:
                removed = db.execute("DELETE FROM iphone_push_registration WHERE token=?", (row["token"],))
                if removed.rowcount:
                    db.execute("DELETE FROM iphone_push_session")
                db.execute("DELETE FROM goal_push_delivery WHERE token=?", (row["token"],))
            elif retry:
                delay = min(900, 5 * 2 ** min(row["attempts"], 8))
                db.execute("UPDATE goal_push_delivery SET attempts=attempts+1,next_try=? WHERE id=? AND token=?",
                           (now + delay, row["id"], row["token"]))
            else:
                db.execute("DELETE FROM goal_push_delivery WHERE id=? AND token=?", (row["id"], row["token"]))
