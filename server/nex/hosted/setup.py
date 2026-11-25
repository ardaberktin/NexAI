"""Bootstrap the first owner using host access or an unlocked legacy device."""

import hmac
import secrets
import sqlite3
from time import time

from ..privacy.state import AccessDenied
from .auth import (
    SETUP_LIFETIME,
    TOKEN,
    device_public,
    hash_token,
    insert_user,
    issue_device,
    password_record,
    username,
)


def owner_exists(db):
    return db.execute("SELECT 1 FROM users WHERE owner=1").fetchone() is not None


def setup_secret(accounts):
    secret = secrets.token_urlsafe(32)
    with accounts.connect(write=True) as db:
        if owner_exists(db):
            raise ValueError("The server already has an owner account.")
        db.execute("INSERT OR REPLACE INTO owner_setup VALUES(1,?,?)", (hash_token(secret), time() + SETUP_LIFETIME))
    return secret


def check_setup(db, secret, legacy_device):
    if owner_exists(db):
        raise AccessDenied("This Nex server already has an owner. Sign in to the existing account.")
    if legacy_device:
        return
    pending = db.execute("SELECT token,expires FROM owner_setup").fetchone()
    if (not isinstance(secret, str) or not TOKEN.fullmatch(secret) or not pending
            or pending["expires"] <= time() or not hmac.compare_digest(pending["token"], hash_token(secret))):
        raise AccessDenied("The setup link is invalid or expired. Export a new link on the server.")


def claim_owner(accounts, body, peer, *, legacy_device=None):
    role, public = device_public(body)
    if legacy_device and legacy_device != role:
        raise AccessDenied("Set up the owner account on the authorized device.")
    secret = body.get("setup")
    name = username(body.get("username"))
    with accounts.password_work(peer):
        with accounts.connect() as db:
            check_setup(db, secret, legacy_device)
        record = password_record(body.get("password"))
        try:
            with accounts.connect(write=True) as db:
                check_setup(db, secret, legacy_device)
                identifier = insert_user(db, name, record, owner=True)
                db.execute("DELETE FROM owner_setup")
                return issue_device(db, identifier, name, True, role, public)
        except sqlite3.IntegrityError:
            raise ValueError("This username is already in use.") from None
