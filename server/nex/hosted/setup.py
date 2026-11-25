"""Bootstrap the first owner through the host's private setup routes."""

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


def ensure_unclaimed(db):
    if owner_exists(db):
        raise AccessDenied("This Nex server already has an owner. Sign in to the existing account.")


def insert_owner(db, name, record):
    try:
        identifier = insert_user(db, name, record, owner=True)
    except sqlite3.IntegrityError:
        raise ValueError("This username is already in use.") from None
    db.execute("DELETE FROM owner_setup")
    return identifier


def create_browser_owner(accounts, body, peer):
    name = username(body.get("username"))
    with accounts.password_work(peer):
        with accounts.connect() as db:
            ensure_unclaimed(db)
        record = password_record(body.get("password"))
        with accounts.connect(write=True) as db:
            ensure_unclaimed(db)
            insert_owner(db, name, record)
    return name


def setup_secret(accounts):
    secret = secrets.token_urlsafe(32)
    with accounts.connect(write=True) as db:
        if owner_exists(db):
            raise ValueError("The server already has an owner account.")
        db.execute("INSERT OR REPLACE INTO owner_setup VALUES(1,?,?)", (hash_token(secret), time() + SETUP_LIFETIME))
    return secret


def check_setup(db, secret):
    ensure_unclaimed(db)
    pending = db.execute("SELECT token,expires FROM owner_setup").fetchone()
    if (not isinstance(secret, str) or not TOKEN.fullmatch(secret) or not pending
            or pending["expires"] <= time() or not hmac.compare_digest(pending["token"], hash_token(secret))):
        raise AccessDenied("The setup link is invalid or expired. Export a new link on the server.")


def claim_owner(accounts, body, peer):
    role, public = device_public(body)
    secret = body.get("setup")
    name = username(body.get("username"))
    with accounts.password_work(peer):
        with accounts.connect() as db:
            check_setup(db, secret)
        record = password_record(body.get("password"))
        with accounts.connect(write=True) as db:
            check_setup(db, secret)
            identifier = insert_owner(db, name, record)
            return issue_device(db, identifier, name, True, role, public)
