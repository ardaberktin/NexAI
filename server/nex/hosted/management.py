"""Account settings authorized against the current device and registry roles."""

import hmac
import sqlite3
from contextlib import contextmanager
from time import time

from ..privacy.state import AccessDenied
from .auth import (
    account,
    derive,
    insert_user,
    password_bytes,
    password_record,
    username,
)


def user_summary(row):
    user = account(row)
    return {"id": user.id, "username": user.username, "owner": user.owner,
            "admin": user.can_manage, "enabled": not row["disabled"]}


class AccountSettings:
    def __init__(self, accounts, client, peer):
        self.accounts, self.client, self.peer = accounts, client, peer

    @contextmanager
    def authorized(self, *, write=False, admin=False):
        with self.accounts.connect(write=write) as db:
            if not write:
                db.execute("BEGIN")
            actor = db.execute(
                """SELECT u.* FROM users u JOIN devices d ON d.user_id=u.id
                   WHERE u.id=? AND d.id=? AND d.expires>? AND u.disabled=0""",
                (self.client.account.id, self.client.id, time()),
            ).fetchone()
            if actor is None:
                raise AccessDenied("This device session has ended. Sign in again.")
            if admin and not (actor["owner"] or actor["admin"]):
                raise AccessDenied("A server administrator is required.")
            yield db, actor

    def profile(self):
        with self.authorized() as (db, actor):
            devices = [{"id": row["id"], "role": row["role"], "current": row["id"] == self.client.id}
                       for row in db.execute("SELECT id,role FROM devices WHERE user_id=? AND expires>? ORDER BY role",
                                             (actor["id"], time()))]
            return user_summary(actor) | {"devices": devices}

    def rename(self, body):
        name = username(body.get("username"))
        try:
            with self.authorized(write=True) as (db, actor):
                db.execute("UPDATE users SET username=? WHERE id=?", (name, actor["id"]))
        except sqlite3.IntegrityError:
            raise ValueError("This username is already in use.") from None
        return self.profile()

    def change_password(self, body):
        old = password_bytes(body.get("current_password"))
        with self.accounts.password_work(self.peer):
            with self.authorized() as (_, actor):
                expected = actor["password_hash"]
                if not hmac.compare_digest(derive(old, actor["salt"]), expected):
                    raise AccessDenied("The current password is incorrect.")
            record = password_record(body.get("password"))
            with self.authorized(write=True) as (db, actor):
                if actor["password_hash"] != expected:
                    raise AccessDenied("The password changed. Try again with the current password.")
                db.execute("UPDATE users SET salt=?,password_hash=? WHERE id=?", (*record, actor["id"]))
                db.execute("DELETE FROM devices WHERE user_id=? AND id<>?", (actor["id"], self.client.id))
        return self.profile()

    def revoke_device(self, body):
        identifier = body.get("device_id")
        if not isinstance(identifier, str) or len(identifier) != 32 or identifier == self.client.id:
            raise ValueError("Choose another registered device.")
        with self.authorized(write=True) as (db, actor):
            db.execute("DELETE FROM devices WHERE user_id=? AND id=?", (actor["id"], identifier))
        return self.profile()

    def users(self):
        with self.authorized(admin=True) as (db, _):
            return {"items": [user_summary(row) for row in db.execute("SELECT * FROM users ORDER BY username")]}

    def create_user(self, body):
        with self.authorized(admin=True):
            name = username(body.get("username"))
        with self.accounts.password_work(self.peer):
            record = password_record(body.get("password"))
            try:
                with self.authorized(write=True, admin=True) as (db, _):
                    identifier = insert_user(db, name, record)
                    return user_summary(db.execute("SELECT * FROM users WHERE id=?", (identifier,)).fetchone())
            except sqlite3.IntegrityError:
                raise ValueError("This username is already in use.") from None

    @staticmethod
    def target(db, actor, identifier, *, allow_owner=False):
        target = db.execute("SELECT * FROM users WHERE id=?", (identifier,)).fetchone()
        if target is None:
            raise ValueError("Choose an existing Nex account.")
        if target["owner"] and not (allow_owner and actor["owner"]):
            raise AccessDenied("Use your own account settings to edit the owner account.")
        if (target["admin"] or target["id"] == actor["id"]) and not actor["owner"]:
            raise AccessDenied("Only the owner can manage administrators.")
        return target

    def update_user(self, identifier, body):
        name = username(body.get("username"))
        enabled, admin = body.get("enabled"), body.get("admin")
        if type(enabled) is not bool or type(admin) is not bool:
            raise ValueError("Choose an account status and role.")
        try:
            with self.authorized(write=True, admin=True) as (db, actor):
                self.target(db, actor, identifier)
                if admin and not actor["owner"]:
                    raise AccessDenied("Only the owner can appoint administrators.")
                db.execute("UPDATE users SET username=?,disabled=?,admin=? WHERE id=?",
                           (name, not enabled, admin, identifier))
                if not enabled:
                    db.execute("DELETE FROM devices WHERE user_id=?", (identifier,))
                return user_summary(db.execute("SELECT * FROM users WHERE id=?", (identifier,)).fetchone())
        except sqlite3.IntegrityError:
            raise ValueError("This username is already in use.") from None

    def reset_password(self, identifier, body):
        with self.authorized(admin=True) as (db, actor):
            self.target(db, actor, identifier)
        with self.accounts.password_work(self.peer):
            record = password_record(body.get("password"))
            with self.authorized(write=True, admin=True) as (db, actor):
                self.target(db, actor, identifier)
                db.execute("UPDATE users SET salt=?,password_hash=? WHERE id=?", (*record, identifier))
                db.execute("DELETE FROM devices WHERE user_id=?", (identifier,))
        return {"ok": True}

    def storage(self, hosted, identifier, options=None):
        with self.authorized(write=options is not None, admin=True) as (db, actor):
            target = self.target(db, actor, identifier, allow_owner=True)
            archive = hosted.workspace(account(target)).store.archive
            if options is None:
                return archive.settings()
            return archive.set_limit(options.get("audio_limit_gib"))
