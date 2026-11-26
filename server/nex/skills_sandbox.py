"""Per-account persistent sandbox workspace and user-approved reusable Python Skills."""

import ast
import asyncio
import json
import re
import signal
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from .coding.guest import MIB, READY
from .coding.sandbox import MAX_OUTPUT, MAX_SOURCE, SLOTS, Run, capture

SKILL_NAME = re.compile(r"^[a-z][a-z0-9_]{1,39}$")
PERSISTENT_QUOTA_BYTES = 32 * MIB
PERSISTENT_WALL_SECONDS = 15

SKILLS_SCHEMA = """
CREATE TABLE IF NOT EXISTS workspace_skills (
    id TEXT PRIMARY KEY,
    name TEXT UNIQUE NOT NULL,
    description TEXT NOT NULL,
    parameters_json TEXT NOT NULL DEFAULT '{}',
    source TEXT NOT NULL,
    approved INTEGER NOT NULL DEFAULT 0,
    created TEXT NOT NULL,
    updated TEXT NOT NULL,
    last_run TEXT,
    run_count INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS workspace_skills_approved ON workspace_skills(approved, name);
"""


def _stamp():
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def workspace_dir(account_root):
    folder = Path(account_root) / "sandbox_files"
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    return folder


def _directory_bytes(path):
    total = 0
    for item in path.rglob("*"):
        if item.is_file():
            total += item.stat().st_size
    return total


def persistent_sandbox_command(work_folder, *, input_json=None):
    env_args = ["--setenv", "HOME", "/work", "--setenv", "LC_ALL", "C.UTF-8"]
    if input_json is not None:
        env_args.extend(["--setenv", "NEX_SKILL_INPUT", input_json])
    return (
        "/usr/bin/bwrap", "--unshare-all", "--unshare-user", "--disable-userns",
        "--new-session", "--die-with-parent", "--cap-drop", "ALL",
        "--uid", "65534", "--gid", "65534", "--hostname", "nex-skill",
        "--clearenv", *env_args,
        "--ro-bind", "/usr/bin/python3", "/usr/bin/python3",
        "--ro-bind", "/usr/lib", "/usr/lib", "--symlink", "usr/lib", "/lib",
        "--ro-bind-try", "/usr/lib64", "/usr/lib64", "--symlink", "usr/lib64", "/lib64",
        "--ro-bind-try", "/usr/share/zoneinfo", "/usr/share/zoneinfo",
        "--ro-bind", str((Path(__file__).parent / "coding" / "guest.py").resolve()), "/runner.py",
        "--dev-bind", "/dev/null", "/dev/null",
        "--bind", str(work_folder.resolve()), "/work", "--symlink", "work", "/tmp",
        "--remount-ro", "/", "--chdir", "/work",
        "--", "/usr/bin/python3", "-I", "-S", "-B", "-u", "/runner.py",
    )


async def run_persistent_python(account_root, source, *, timeout=PERSISTENT_WALL_SECONDS, input_data=None):
    """Run a network-isolated Python script with a bounded per-account persistent /work directory."""
    data = source.encode("utf-8")
    if len(data) > MAX_SOURCE:
        return Run("source_limit")
    folder = workspace_dir(account_root)
    if _directory_bytes(folder) > PERSISTENT_QUOTA_BYTES:
        return Run("quota_exceeded", "The persistent sandbox workspace has reached its 32 MiB limit.")
    input_json = json.dumps(input_data, ensure_ascii=False) if input_data is not None else None
    if input_json and len(input_json.encode("utf-8")) > 16_384:
        return Run("input_limit")
    if not SLOTS.acquire(blocking=False):
        return Run("busy")
    try:
        process = await asyncio.create_subprocess_exec(
            *persistent_sandbox_command(folder, input_json=input_json),
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT, env={}, limit=MAX_OUTPUT,
        )
        try:
            async with asyncio.timeout(timeout):
                return await capture(process, data)
        except TimeoutError:
            return Run("timeout")
        finally:
            if process.returncode is None:
                with suppress(ProcessLookupError):
                    process.kill()
            await process.communicate()
    except OSError:
        return Run("unavailable")
    finally:
        SLOTS.release()


def _row_dict(row):
    return {
        "id": row["id"],
        "name": row["name"],
        "description": row["description"],
        "parameters": json.loads(row["parameters_json"]),
        "source": row["source"],
        "approved": bool(row["approved"]),
        "created": row["created"],
        "updated": row["updated"],
        "last_run": row["last_run"],
        "run_count": row["run_count"],
    }


class SkillsManager:
    """Manage user-approved reusable Python skills stored per account."""

    def __init__(self, store):
        self.store = store

    def propose(self, name, description, source, *, parameters=None):
        if not isinstance(name, str) or not SKILL_NAME.fullmatch(name):
            raise ValueError("Skill name must be 2–40 lowercase letters, digits, or underscores.")
        if not isinstance(description, str) or not 4 <= len(description.strip()) <= 240:
            raise ValueError("Provide a short skill description (4–240 characters).")
        if not isinstance(source, str) or not 1 <= len(source.encode("utf-8")) <= MAX_SOURCE:
            raise ValueError("Skill Python source must be between 1 and 12,000 bytes.")
        try:
            ast.parse(source)
        except SyntaxError as exc:
            raise ValueError(f"Skill Python syntax error: {exc.msg}") from None
        schema = parameters if isinstance(parameters, dict) else {"type": "object", "properties": {}}
        stamp = _stamp()
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute("SELECT id FROM workspace_skills WHERE name=?", (name,)).fetchone()
            if existing:
                skill_id = existing["id"]
                db.execute(
                    """UPDATE workspace_skills
                       SET description=?, parameters_json=?, source=?, approved=0, updated=?
                       WHERE id=?""",
                    (description.strip(), json.dumps(schema, ensure_ascii=False), source, stamp, skill_id),
                )
            else:
                skill_id = str(uuid4())
                db.execute(
                    """INSERT INTO workspace_skills(
                        id, name, description, parameters_json, source, approved, created, updated
                    ) VALUES(?,?,?,?,?,0,?,?)""",
                    (skill_id, name, description.strip(), json.dumps(schema, ensure_ascii=False), source, stamp, stamp),
                )
            row = db.execute("SELECT * FROM workspace_skills WHERE id=?", (skill_id,)).fetchone()
        return _row_dict(row)

    def list(self, *, approved_only=False):
        with self.store.connect() as db:
            if approved_only:
                rows = db.execute("SELECT * FROM workspace_skills WHERE approved=1 ORDER BY name").fetchall()
            else:
                rows = db.execute("SELECT * FROM workspace_skills ORDER BY approved DESC, name").fetchall()
        return [_row_dict(row) for row in rows]

    def get(self, identifier):
        with self.store.connect() as db:
            row = db.execute(
                "SELECT * FROM workspace_skills WHERE id=? OR name=?",
                (identifier, identifier),
            ).fetchone()
        return _row_dict(row) if row else None

    def approve(self, identifier):
        stamp = _stamp()
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT id FROM workspace_skills WHERE id=? OR name=?", (identifier, identifier)).fetchone()
            if row is None:
                raise LookupError("Skill not found.")
            db.execute("UPDATE workspace_skills SET approved=1, updated=? WHERE id=?", (stamp, row["id"]))
            updated = db.execute("SELECT * FROM workspace_skills WHERE id=?", (row["id"],)).fetchone()
        return _row_dict(updated)

    def delete(self, identifier):
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            deleted = db.execute("DELETE FROM workspace_skills WHERE id=? OR name=?", (identifier, identifier))
        if not deleted.rowcount:
            raise LookupError("Skill not found.")
        return {"deleted": True}

    async def execute(self, name, arguments=None):
        skill = self.get(name)
        if skill is None:
            return {"status": "unavailable", "detail": f"Unknown skill: {name}"}
        if not skill["approved"]:
            return {"status": "approval_required", "skill_id": skill["id"], "name": skill["name"],
                    "detail": "This skill requires owner approval before it can run."}
        run = await run_persistent_python(self.store.root, skill["source"], input_data=arguments or {})
        if run.status == "ok":
            with self.store.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                db.execute(
                    "UPDATE workspace_skills SET last_run=?, run_count=run_count+1 WHERE id=?",
                    (_stamp(), skill["id"]),
                )
        return {"skill": skill["name"], "status": run.status, "output": run.output, "exit_code": run.exit_code}


__all__ = ["READY", "SKILLS_SCHEMA", "SkillsManager", "run_persistent_python", "signal"]
