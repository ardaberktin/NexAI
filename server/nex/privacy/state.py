"""Cross-process privacy state, separate from searchable chat and recordings."""

import hashlib
import sqlite3
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS keys(device TEXT PRIMARY KEY, public BLOB, bootstrap TEXT);
CREATE TABLE IF NOT EXISTS challenges(id TEXT PRIMARY KEY, device TEXT, purpose TEXT, subject TEXT, expires REAL);
CREATE TABLE IF NOT EXISTS sessions(token TEXT PRIMARY KEY, device TEXT, expires REAL);
CREATE TABLE IF NOT EXISTS session_devices(
    token TEXT PRIMARY KEY REFERENCES sessions(token) ON DELETE CASCADE, device_id TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS requests(
    id TEXT PRIMARY KEY, device TEXT, payload TEXT, speaker TEXT, created REAL, state TEXT,
    updated REAL, answer TEXT
);
CREATE TABLE IF NOT EXISTS voices(device TEXT PRIMARY KEY, template BLOB NOT NULL);
CREATE TABLE IF NOT EXISTS voice_changes(device TEXT PRIMARY KEY, nonce TEXT);
CREATE TABLE IF NOT EXISTS citations(
    request_id TEXT PRIMARY KEY REFERENCES requests(id) ON DELETE CASCADE, sources TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS request_goals(
    request_id TEXT PRIMARY KEY REFERENCES requests(id) ON DELETE CASCADE, goal_id TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS request_recall(
    request_id TEXT PRIMARY KEY REFERENCES requests(id) ON DELETE CASCADE, cursor TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS request_goal_choices(
    request_id TEXT PRIMARY KEY REFERENCES requests(id) ON DELETE CASCADE, choices TEXT NOT NULL
);
"""


class AccessDenied(Exception):
    pass


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def approval_subject(row):
    return row["id"] + ":" + digest(f'{row["created"]}\n{row["device"]}\n{row["payload"]}')


class PrivacyState:
    def __init__(self, root):
        self.root = Path(root)
        with self.transaction() as db:
            db.executescript(SCHEMA)

    @contextmanager
    def transaction(self, *, write=False):
        db = sqlite3.connect(self.root / "privacy.sqlite", timeout=10)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA foreign_keys=ON")
            with db:
                if write:
                    db.execute("BEGIN IMMEDIATE")
                yield db
        finally:
            db.close()
