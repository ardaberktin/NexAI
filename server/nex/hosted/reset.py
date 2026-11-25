"""Erase personal data after both Nex services have stopped using the archive."""

import fcntl
import os
import re
import sqlite3
import sys
from contextlib import closing, contextmanager
from pathlib import Path
from shutil import rmtree
from time import sleep
from uuid import uuid4

from ..audio_files import sync_directory
from .auth import Accounts

OWNER_DATA = ("accounts", "audio", "context", "recovery", "memory.sqlite", "privacy.sqlite")
SQLITE_SIDECARS = ("-wal", "-shm", "-journal")
ACCOUNT_ID = re.compile(r"[0-9a-f]{32}")
RECORDING_ID = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}")


def remove(path):
    if path.is_dir() and not path.is_symlink():
        rmtree(path)
    else:
        path.unlink(missing_ok=True)


def remove_database(path):
    for suffix in ("", *SQLITE_SIDECARS):
        path.with_name(path.name + suffix).unlink(missing_ok=True)


def owner_recording_ids(root):
    audio = root / "audio"
    identifiers = set() if audio.is_symlink() else {entry.stem for entry in audio.glob("*.m4a")}
    database = root / "memory.sqlite"
    if database.is_file():
        with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as db:
            identifiers.update(row[0] for row in db.execute("SELECT id FROM recordings"))
    return {identifier for identifier in identifiers if isinstance(identifier, str) and RECORDING_ID.fullmatch(identifier)}


def erase_owner(root, ssd_audio):
    if ssd_audio is not None:
        for identifier in owner_recording_ids(root):
            (ssd_audio / (identifier + ".m4a")).unlink(missing_ok=True)
        sync_directory(ssd_audio)
    for name in OWNER_DATA:
        path = root / name
        if path.suffix == ".sqlite":
            remove_database(path)
        else:
            remove(path)
    sync_directory(root)


def finish_resets(root, accounts, ssd_audio):
    with accounts.connect() as db:
        pending = db.execute("SELECT u.id,u.owner FROM account_resets r JOIN users u ON u.id=r.user_id").fetchall()
    for identifier, owner in pending:
        if owner:
            erase_owner(root, ssd_audio)
        else:
            if not ACCOUNT_ID.fullmatch(identifier):
                raise ValueError("The pending account identifier is invalid.")
            members = root / "users"
            if members.is_symlink():
                raise ValueError("The Nex users directory must be inside the archive.")
            remove(members / identifier)
            if members.is_dir():
                sync_directory(members)
        with accounts.connect(write=True) as db:
            db.execute("UPDATE users SET archive_id=? WHERE id=?", (uuid4().hex, identifier))
            db.execute("DELETE FROM account_resets WHERE user_id=?", (identifier,))


@contextmanager
def workspace_lock(root, *, archive=False, ssd_audio=None):
    # Both services hold a shared lock until process exit or exec. Only the archive service
    # can take the exclusive lock and erase data because it knows the configured SSD path.
    with (Path(root) / ".workspace.lock").open("ab") as lock:
        while True:
            fcntl.flock(lock, fcntl.LOCK_SH)
            accounts = Accounts(root)
            if not accounts.resets_pending():
                break
            fcntl.flock(lock, fcntl.LOCK_UN)
            if archive:
                fcntl.flock(lock, fcntl.LOCK_EX)
                finish_resets(root, accounts, ssd_audio)
                fcntl.flock(lock, fcntl.LOCK_UN)
            else:
                sleep(0.25)
        yield accounts


def restart_process():
    os.execv(sys.executable, [sys.executable, *sys.orig_argv[1:]])
