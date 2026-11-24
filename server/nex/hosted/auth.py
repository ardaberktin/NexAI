"""Local account credentials and revocable device sessions shared by both listeners."""

import base64
import hmac
import re
import secrets
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from hashlib import scrypt, sha256
from time import time
from uuid import uuid4

DEVICES = ("mac", "iphone")
LIFETIME = 90 * 86400
USERNAME = re.compile(r"[a-z0-9][a-z0-9._-]{2,63}")
TOKEN = re.compile(r"[A-Za-z0-9_-]{43}")
SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY, username TEXT UNIQUE NOT NULL,
    salt BLOB NOT NULL, password_hash BLOB NOT NULL,
    owner INTEGER NOT NULL, disabled INTEGER NOT NULL DEFAULT 0
);
CREATE UNIQUE INDEX IF NOT EXISTS single_owner ON users(owner) WHERE owner=1;
CREATE TABLE IF NOT EXISTS devices (
    token BLOB PRIMARY KEY, id TEXT UNIQUE NOT NULL, user_id TEXT NOT NULL REFERENCES users(id),
    role TEXT NOT NULL, public BLOB NOT NULL, expires REAL NOT NULL,
    UNIQUE(user_id,role)
);
CREATE TABLE IF NOT EXISTS attempts (peer TEXT PRIMARY KEY, count INTEGER NOT NULL, resets REAL NOT NULL);
"""


def username(value):
    value = value.strip().lower() if isinstance(value, str) and value.isascii() else ""
    if not USERNAME.fullmatch(value):
        raise ValueError("Use a username with 3–64 letters, digits, periods, underscores or hyphens.")
    return value


def password_bytes(value, minimum=1):
    if not isinstance(value, str) or len(value) < minimum or len(value.encode()) > 512:
        raise ValueError("Use a password of at least 12 characters and at most 512 UTF-8 bytes.")
    return value.encode()


def derive(password, salt):
    # OWASP scrypt profile with a 64 MiB working set.
    return scrypt(password, salt=salt, n=2**16, r=8, p=2, maxmem=128 * 2**20, dklen=32)


def hash_token(token):
    return sha256(token.encode()).digest()


class BadLogin(Exception):
    pass


class LoginBusy(Exception):
    pass


@dataclass(frozen=True)
class Account:
    id: str
    username: str
    owner: bool

    def root(self, host):
        return host if self.owner else host / "users" / self.id

    def speaker_socket(self, runtime):
        return runtime / ("speaker.sock" if self.owner else f"s-{self.id}.sock")


@dataclass(frozen=True)
class Device:
    account: Account
    id: str
    role: str
    public: bytes


def account(row):
    return Account(row["id"], row["username"], bool(row["owner"]))


class Accounts:
    def __init__(self, root):
        self.path = root / "accounts.sqlite"
        # The host's private data directory and its 0077 umask also protect SQLite journals.
        self.path.touch(mode=0o600, exist_ok=True)
        self.slots = threading.BoundedSemaphore(2)
        with self.connect() as db:
            db.executescript(SCHEMA)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA foreign_keys=ON")
            with db:
                yield db
        finally:
            db.close()

    def create(self, name, password, *, owner=False):
        name = username(name)
        salt = secrets.token_bytes(16)
        hashed = derive(password_bytes(password, minimum=12), salt)
        try:
            with self.connect() as db:
                db.execute("INSERT INTO users(id,username,salt,password_hash,owner) VALUES(?,?,?,?,?)",
                           (uuid4().hex, name, salt, hashed, owner))
        except sqlite3.IntegrityError:
            raise ValueError("That username or the existing archive's owner is already registered.") from None

    def reset(self, name, password):
        salt = secrets.token_bytes(16)
        hashed = derive(password_bytes(password, minimum=12), salt)
        with self.connect() as db:
            user = db.execute("UPDATE users SET salt=?,password_hash=? WHERE username=? RETURNING id",
                              (salt, hashed, username(name))).fetchone()
            if user is None:
                raise ValueError("Unknown Nex account.")
            db.execute("DELETE FROM devices WHERE user_id=?", (user["id"],))

    def set_enabled(self, name, enabled):
        with self.connect() as db:
            user = db.execute("UPDATE users SET disabled=? WHERE username=? RETURNING id",
                              (not enabled, username(name))).fetchone()
            if user is None:
                raise ValueError("Unknown Nex account.")
            if not enabled:
                db.execute("DELETE FROM devices WHERE user_id=?", (user["id"],))

    def list(self, *, active=False):
        with self.connect() as db:
            return [(account(row), not row["disabled"]) for row in db.execute(
                "SELECT * FROM users WHERE disabled=0 OR ? ORDER BY username", (not active,))]

    def legacy_allowed(self):
        with self.connect() as db:
            return db.execute("SELECT 1 FROM users WHERE owner=1").fetchone() is None

    def authenticate(self, token):
        if not TOKEN.fullmatch(token):
            return None
        with self.connect() as db:
            row = db.execute(
                """SELECT u.*, d.id AS device_id,d.role,d.public FROM devices d JOIN users u ON u.id=d.user_id
                   WHERE d.token=? AND d.expires>? AND u.disabled=0""", (hash_token(token), time())
            ).fetchone()
        return Device(account(row), row["device_id"], row["role"], row["public"]) if row else None

    def device_active(self, device_id):
        with self.connect() as db:
            return db.execute(
                """SELECT 1 FROM devices d JOIN users u ON u.id=d.user_id
                   WHERE d.id=? AND d.expires>? AND u.disabled=0""", (device_id, time())
            ).fetchone() is not None

    def revoke(self, token):
        with self.connect() as db:
            db.execute("DELETE FROM devices WHERE token=?", (hash_token(token),))

    def charge_attempt(self, peer):
        now = time()
        with self.connect() as db:
            db.execute("DELETE FROM attempts WHERE resets<=?", (now,))
            row = db.execute(
                """INSERT INTO attempts VALUES(?,1,?) ON CONFLICT(peer)
                   DO UPDATE SET count=count+1 WHERE count<8 RETURNING count""",
                (peer, now + 60),
            ).fetchone()
            if row is None:
                raise LoginBusy

    def sign_in(self, body, peer):
        if not self.slots.acquire(blocking=False):
            raise LoginBusy
        try:
            self.charge_attempt(peer)
            return self.verify_login(body)
        finally:
            self.slots.release()

    def verify_login(self, body):
        role = body.get("device")
        try:
            name = username(body.get("username"))
            password = password_bytes(body.get("password"))
            public = base64.b64decode(body.get("public", ""), validate=True)
            if role not in DEVICES or len(public) != 65 or public[0] != 4:
                raise ValueError
        except (ValueError, TypeError):
            raise BadLogin from None
        with self.connect() as db:
            user = db.execute("SELECT * FROM users WHERE username=?", (name,)).fetchone()
        calculated = derive(password, user["salt"] if user else b"Nex login v1____")
        matches = hmac.compare_digest(calculated, user["password_hash"] if user else bytes(32))
        if not matches or user is None or user["disabled"]:
            raise BadLogin
        token, identifier = secrets.token_urlsafe(32), uuid4().hex
        with self.connect() as db:
            # The compare and replacement are atomic with password resets and disabling an account.
            db.execute("BEGIN IMMEDIATE")
            current = db.execute("SELECT password_hash,disabled FROM users WHERE id=?", (user["id"],)).fetchone()
            if current["disabled"] or current["password_hash"] != user["password_hash"]:
                raise BadLogin
            db.execute("INSERT OR REPLACE INTO devices VALUES(?,?,?,?,?,?)",
                       (hash_token(token), identifier, user["id"], role, public, time() + LIFETIME))
        return {"token": token, "device_id": identifier, "account_id": user["id"],
                "username": user["username"], "owner": bool(user["owner"])}


def bearer(header):
    return header.removeprefix("Bearer ") if header.startswith("Bearer ") else ""


def authenticate_client(accounts, keys, token):
    if accounts and (device := accounts.authenticate(token)):
        return device.role, device
    if token:
        encoded = token.encode()
        role = next((role for role in DEVICES if hmac.compare_digest(encoded, keys[role].encode())), None)
        if role and (accounts is None or accounts.legacy_allowed()):
            return role, None
    return None, None
