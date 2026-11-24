"""Atomic, shared Markdown files for Nex's personality and personal context."""

import fcntl
import hashlib
import os
import re
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import NamedTemporaryFile

from .recall_dates import time_key
from .store import excerpt, query_terms

DEFAULTS = {
    "now": "# Now\n",
    "memory": "# Memory\n\n## Personal notes\n",
    "soul": (
        "# Nex\n\n"
        "Be a calm, practical personal assistant. Speak naturally and keep everyday replies concise. "
        "Think through what the user is trying to accomplish and offer concrete help. "
        "State uncertainty plainly and distinguish observations from assumptions. "
        "Use the language of the current conversation.\n"
    ),
}
MAX_FILE = 16_000
LEARNED = "## Learned from conversations"
FRESHNESS = {"Mac screen": timedelta(minutes=20), "Conversation": timedelta(hours=6),
             "Daybook activity": timedelta(hours=6)}
SECTIONS = re.compile(r"(?=^## )", re.MULTILINE)
FACT = re.compile(r"^- \*\*([\w -]+)\*\* — (.+)$")
SECRET = re.compile(r"\b(?:password|passphrase|secret key|api key|access token|private key|recovery phrase)\b", re.IGNORECASE)


class PersonalConflict(Exception):
    pass


def safe_line(text, limit):
    if not isinstance(text, str) or SECRET.search(text):
        return ""
    return " ".join(text.split())[:limit].strip()


def section_title(section):
    return section.partition("\n")[0].removeprefix("## ")


def signal_time(section):
    line = section.splitlines()[1:2]
    if line:
        try:
            return datetime.fromisoformat(line[0].strip("*")).astimezone(UTC)
        except ValueError:
            pass
    return None


class PersonalFiles:
    def __init__(self, root):
        self.root = Path(root) / "context"
        self.root.mkdir(mode=0o700, exist_ok=True)

    @contextmanager
    def locked(self):
        with (self.root / ".lock").open("ab") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield

    def path(self, name):
        if name not in DEFAULTS:
            raise ValueError("Choose NOW.md, MEMORY.md or SOUL.md.")
        return self.root / (name.upper() + ".md")

    def write(self, path, text):
        with NamedTemporaryFile(dir=self.root, prefix=".write-", delete=False) as temporary:
            staging = Path(temporary.name)
            try:
                temporary.write(text.encode())
                temporary.flush()
                os.fsync(temporary.fileno())
                os.replace(staging, path)
            finally:
                staging.unlink(missing_ok=True)

    def read(self, name):
        path = self.path(name)
        if not path.exists():
            self.write(path, DEFAULTS[name])
        with path.open("rb") as stream:
            data = stream.read(MAX_FILE + 1)
            modified = datetime.fromtimestamp(os.fstat(stream.fileno()).st_mtime, UTC)
        if len(data) > MAX_FILE:
            raise ValueError("Keep each context file under 16 KB.")
        return {"name": name, "markdown": data.decode(), "revision": hashlib.sha256(data).hexdigest(),
                "modified": time_key(modified)}

    def current(self, anchor):
        found = self.read("now")
        kept = []
        for part in SECTIONS.split(found["markdown"]):
            lifetime = FRESHNESS.get(section_title(part))
            observed = signal_time(part) if lifetime else None
            if observed is None or anchor - observed < lifetime:
                kept.append(part)
        text = "".join(kept)
        if text != found["markdown"]:
            self.write(self.path("now"), text.rstrip() + "\n")
            return self.read("now")
        return found

    def snapshot(self):
        with self.locked():
            return {name: self.current(datetime.now(UTC)) if name == "now" else self.read(name)
                    for name in DEFAULTS}

    def edit(self, name, revision, text):
        if not isinstance(text, str) or len(text.encode()) > MAX_FILE or "\x00" in text:
            raise ValueError("Keep each context file under 16 KB.")
        with self.locked():
            current = self.current(datetime.now(UTC)) if name == "now" else self.read(name)
            if revision != current["revision"]:
                raise PersonalConflict("This file changed on Excalibur. Reload it before saving.")
            if text != current["markdown"]:
                self.write(self.path(name), text)
            return self.read(name)

    def append(self, name, text, *, heading):
        with self.locked():
            current = self.read(name)["markdown"]
            line = "- " + text
            if line in current.splitlines():
                return
            parts = SECTIONS.split(current)
            for i, part in enumerate(parts):
                if section_title(part) == heading[3:]:
                    parts[i] = part.rstrip() + "\n" + line + "\n\n"
                    break
            else:
                parts.extend(("\n\n" if current and not current.endswith("\n\n") else "", heading + "\n" + line))
            result = "".join(parts).rstrip() + "\n"
            if len(result.encode()) > MAX_FILE:
                raise ValueError("This context file is full. Edit it in Memory to make room.")
            self.write(self.path(name), result)

    def signal(self, slot, text, observed):
        if slot not in FRESHNESS:
            raise ValueError("Unknown activity source.")
        clean = safe_line(text, 480)
        anchor = datetime.now(UTC)
        if not clean or not anchor - FRESHNESS[slot] < observed <= anchor + timedelta(minutes=2):
            return
        with self.locked():
            current = self.current(anchor)["markdown"]
            segments = SECTIONS.split(current)
            for part in segments:
                if section_title(part) == slot and (previous := signal_time(part)) and previous >= observed:
                    return
            kept = [part for part in segments if section_title(part) != slot]
            result = "".join(kept).rstrip() + f"\n\n## {slot}\n*{time_key(observed)}*\n{clean}\n"
            if len(result.encode()) <= MAX_FILE:
                self.write(self.path("now"), result)

    def learn(self, facts, revision):
        if not facts:
            return
        with self.locked():
            document = self.read("memory")
            if document["revision"] != revision:
                return
            parts = SECTIONS.split(document["markdown"])
            index = next((i for i, part in enumerate(parts) if section_title(part) == LEARNED[3:]), len(parts))
            current = parts[index] if index < len(parts) else LEARNED
            lines = current.rstrip().splitlines()
            keys = {match[1].casefold(): i for i, line in enumerate(lines) if (match := FACT.fullmatch(line))}
            for key, text in facts:
                line = f"- **{key}** — {text}"
                if key.casefold() in keys:
                    lines[keys[key.casefold()]] = line
                elif len(keys) < 32:
                    keys[key.casefold()] = len(lines)
                    lines.append(line)
            parts[index:index + 1] = ["\n".join(lines).rstrip() + "\n\n"]
            if index == len(parts) - 1 and index and not parts[index - 1].endswith("\n\n"):
                parts[index - 1] = parts[index - 1].rstrip() + "\n\n"
            result = "".join(parts).rstrip() + "\n"
            if result != document["markdown"] and len(result.encode()) <= MAX_FILE:
                self.write(self.path("memory"), result)


def relevant_memory(text, question, budget=1350):
    if len(text) <= budget:
        return text.strip()
    terms = set(query_terms(question))
    lines = [line.strip() for line in text.splitlines() if line.strip() and not line.startswith("#")]
    lines.reverse()
    lines.sort(key=lambda line: len(terms.intersection(query_terms(line))), reverse=True)
    return excerpt("\n".join(lines), budget)


def context_excerpt(name, markdown, query, limit=2200):
    if not query:
        return excerpt(markdown, limit)
    if name == "memory":
        return relevant_memory(markdown, query, limit)
    if name == "now":
        terms = set(query_terms(query))
        sections = [part.strip() for part in SECTIONS.split(markdown) if part.strip()]
        sections.sort(key=lambda part: (len(terms.intersection(query_terms(part))),
                                        signal_time(part) or datetime.min.replace(tzinfo=UTC)), reverse=True)
        return excerpt("\n\n".join(sections), limit)
    return excerpt(markdown, limit)


def owner_preferences(store):
    try:
        files = PersonalFiles(store.root)
        with files.locked():
            markdown = files.read("soul")["markdown"]
    except (OSError, ValueError):
        return ""
    return excerpt(markdown.strip(), 1000) if markdown != DEFAULTS["soul"] else ""
