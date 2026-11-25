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
SETUP_LIFETIME = 30 * 60
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
CREATE TABLE IF NOT EXISTS owner_setup (
    singleton INTEGER PRIMARY KEY CHECK(singleton=1), token BLOB NOT NULL, expires REAL NOT NULL
);
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


def password_record(password):
    salt = secrets.token_bytes(16)
    return salt, derive(password_bytes(password, minimum=12), salt)


def device_public(body):
    role = body.get("device")
    public = base64.b64decode(body.get("public", ""), validate=True)
    if role not in DEVICES or len(public) != 65 or public[0] != 4:
        raise ValueError("A valid Nex device key is required.")
    return role, public


def insert_user(db, name, password, *, owner=False):
    identifier = uuid4().hex
    db.execute("INSERT INTO users(id,username,salt,password_hash,owner) VALUES(?,?,?,?,?)",
               (identifier, name, *password, owner))
    return identifier


def issue_device(db, identifier, name, owner, role, public):
    token, device_id = secrets.token_urlsafe(32), uuid4().hex
    db.execute("INSERT OR REPLACE INTO devices VALUES(?,?,?,?,?,?)",
               (hash_token(token), device_id, identifier, role, public, time() + LIFETIME))
    return {"token": token, "device_id": device_id, "account_id": identifier, "username": name, "owner": bool(owner)}


class BadLogin(Exception):
    pass


class LoginBusy(Exception):
    pass


@dataclass(frozen=True)
class Account:
    id: str
    username: str
    owner: bool
    admin: bool = False

    @property
    def can_manage(self):
        return self.owner or self.admin

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
    return Account(row["id"], row["username"], bool(row["owner"]), bool(row["admin"]))


class Accounts:
    def __init__(self, root):
        self.path = root / "accounts.sqlite"
        # The host's private data directory and its 0077 umask also protect SQLite journals.
        self.path.touch(mode=0o600, exist_ok=True)
        self.slots = threading.BoundedSemaphore(2)
        with self.connect() as db:
            db.executescript(SCHEMA)
            db.execute("BEGIN IMMEDIATE")
            if "admin" not in {column[1] for column in db.execute("PRAGMA table_info(users)")}:
                db.execute("ALTER TABLE users ADD COLUMN admin INTEGER NOT NULL DEFAULT 0 CHECK(admin IN (0, 1))")

    @contextmanager
    def connect(self, *, write=False):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA foreign_keys=ON")
            with db:
                if write:
                    db.execute("BEGIN IMMEDIATE")
                yield db
        finally:
            db.close()

    def create(self, name, password, *, owner=False):
        name = username(name)
        record = password_record(password)
        try:
            with self.connect() as db:
                insert_user(db, name, record, owner=owner)
                if owner:
                    db.execute("DELETE FROM owner_setup")
        except sqlite3.IntegrityError:
            raise ValueError("That username or the existing archive's owner is already registered.") from None

    def reset(self, name, password):
        salt, hashed = password_record(password)
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

    @contextmanager
    def password_work(self, peer):
        if not self.slots.acquire(blocking=False):
            raise LoginBusy
        try:
            self.charge_attempt(peer)
            yield
        finally:
            self.slots.release()

    def sign_in(self, body, peer):
        with self.password_work(peer):
            return self.verify_login(body)

    def verify_login(self, body):
        try:
            name = username(body.get("username"))
            password = password_bytes(body.get("password"))
            role, public = device_public(body)
        except (ValueError, TypeError):
            raise BadLogin from None
        with self.connect() as db:
            user = db.execute("SELECT * FROM users WHERE username=?", (name,)).fetchone()
        calculated = derive(password, user["salt"] if user else b"Nex login v1____")
        matches = hmac.compare_digest(calculated, user["password_hash"] if user else bytes(32))
        if not matches or user is None or user["disabled"]:
            raise BadLogin
        with self.connect(write=True) as db:
            # The compare and replacement are atomic with password resets, renames and disabling an account.
            current = db.execute("SELECT username,password_hash,disabled FROM users WHERE id=?", (user["id"],)).fetchone()
            if not current or current["disabled"] or current["password_hash"] != user["password_hash"] or current["username"] != name:
                raise BadLogin
            return issue_device(db, user["id"], name, user["owner"], role, public)


def bearer(header):
    return header.removeprefix("Bearer ") if header.startswith("Bearer ") else ""
