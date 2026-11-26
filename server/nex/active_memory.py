"""Hybrid Active Memory: unlimited structured facts with bilingual FTS5 + vector recall."""

import array
import hashlib
import logging
import math
import re
import sqlite3
from datetime import UTC, datetime
from uuid import uuid4

from .personal_files import FACT, LEARNED, PersonalFiles, safe_line
from .store import excerpt, normalized, query_terms

LOG = logging.getLogger(__name__)
VECTOR_DIM = 128
HASH_MODEL = "bilingual-char3-v1"
VALID_CATEGORIES = frozenset({"fact", "preference", "project", "goal", "person", "note"})

ACTIVE_MEMORY_SCHEMA = """
CREATE TABLE IF NOT EXISTS memory_facts (
    id TEXT PRIMARY KEY,
    fact_key TEXT UNIQUE NOT NULL,
    display_key TEXT NOT NULL,
    category TEXT NOT NULL DEFAULT 'fact',
    content TEXT NOT NULL,
    confidence REAL NOT NULL DEFAULT 1.0,
    source_id TEXT NOT NULL DEFAULT '',
    created TEXT NOT NULL,
    updated TEXT NOT NULL,
    expires_at TEXT,
    active INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS memory_facts_active ON memory_facts(active, updated DESC);
CREATE VIRTUAL TABLE IF NOT EXISTS memory_facts_fts USING fts5(
    display_key, content, category,
    content='memory_facts', content_rowid='rowid',
    tokenize='unicode61 remove_diacritics 2'
);
CREATE TRIGGER IF NOT EXISTS memory_facts_ai AFTER INSERT ON memory_facts BEGIN
    INSERT INTO memory_facts_fts(rowid, display_key, content, category)
    VALUES (new.rowid, new.display_key, new.content, new.category);
END;
CREATE TRIGGER IF NOT EXISTS memory_facts_ad AFTER DELETE ON memory_facts BEGIN
    INSERT INTO memory_facts_fts(memory_facts_fts, rowid, display_key, content, category)
    VALUES ('delete', old.rowid, old.display_key, old.content, old.category);
END;
CREATE TRIGGER IF NOT EXISTS memory_facts_au AFTER UPDATE OF display_key, content, category ON memory_facts BEGIN
    INSERT INTO memory_facts_fts(memory_facts_fts, rowid, display_key, content, category)
    VALUES ('delete', old.rowid, old.display_key, old.content, old.category);
    INSERT INTO memory_facts_fts(rowid, display_key, content, category)
    VALUES (new.rowid, new.display_key, new.content, new.category);
END;
CREATE TABLE IF NOT EXISTS memory_vectors (
    fact_id TEXT PRIMARY KEY REFERENCES memory_facts(id) ON DELETE CASCADE,
    dim INTEGER NOT NULL,
    model TEXT NOT NULL,
    vector BLOB NOT NULL
);
"""


def _stamp():
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def _normalize_vector(values):
    norm = math.sqrt(sum(v * v for v in values))
    if norm <= 1e-12:
        return [0.0] * len(values)
    return [v / norm for v in values]


def pack_vector(values):
    return array.array("f", _normalize_vector(values)).tobytes()


def unpack_vector(blob):
    arr = array.array("f")
    arr.frombytes(blob)
    return arr


def dot_similarity(left, right):
    if len(left) != len(right) or not left:
        return 0.0
    return sum(a * b for a, b in zip(left, right, strict=False))


def bilingual_hash_vector(text, dim=VECTOR_DIM):
    """Deterministic EN+TR character 3-gram + stem projection vector with zero external dependencies."""
    clean = normalized(text or "")
    vec = [0.0] * dim
    tokens = re.findall(r"[a-z0-9]+", clean)
    features = []
    for token in tokens:
        if len(token) >= 2:
            features.append(("w", token, 2.0))
        if len(token) >= 5:
            features.append(("s", token[:5], 1.5))
        padded = f"^{token}$"
        for idx in range(len(padded) - 2):
            features.append(("g", padded[idx:idx + 3], 1.0))
    for kind, feat, weight in features:
        digest = hashlib.blake2b(f"{kind}:{feat}".encode(), digest_size=8).digest()
        bucket = int.from_bytes(digest[:4], "little") % dim
        sign = 1.0 if digest[4] & 1 else -1.0
        vec[bucket] += sign * weight
    return _normalize_vector(vec)


def _fts_query(text):
    terms = query_terms(text)
    if not terms:
        return ""
    return " OR ".join(f'"{term}"*' for term in terms[:10])


class ActiveMemory:
    """Structured memory store with bilingual hybrid retrieval and MEMORY.md mirroring."""

    def __init__(self, store, *, embed_fn=None):
        self.store = store
        self.embed_fn = embed_fn
        self.files = PersonalFiles(store.root)

    def _vector_for(self, text):
        if self.embed_fn is not None:
            try:
                vectors = self.embed_fn([text])
                if vectors and isinstance(vectors[0], list) and len(vectors[0]) >= 16:
                    return "neural", vectors[0]
            except Exception as exc:  # noqa: BLE001 - fall back to local hash vectors when embedder is offline.
                LOG.debug("Active memory neural embedding fallback (%s)", type(exc).__name__)
        return HASH_MODEL, bilingual_hash_vector(text)

    def upsert(self, key, content, *, category="fact", confidence=1.0, source_id="", expires_at=None,
               sync_markdown=True):
        display_key = safe_line(key, 60)
        clean_content = safe_line(content, 500)
        if not display_key or not clean_content:
            return None
        cat = category if category in VALID_CATEGORIES else "fact"
        fact_key = normalized(display_key)
        stamp = _stamp()
        model_name, vec = self._vector_for(f"{display_key}: {clean_content}")
        blob = pack_vector(vec)
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute("SELECT id FROM memory_facts WHERE fact_key=?", (fact_key,)).fetchone()
            if existing:
                fact_id = existing["id"]
                db.execute(
                    """UPDATE memory_facts
                       SET display_key=?, category=?, content=?, confidence=?, source_id=?,
                           updated=?, expires_at=?, active=1
                       WHERE id=?""",
                    (display_key, cat, clean_content, float(confidence), source_id, stamp, expires_at, fact_id),
                )
            else:
                fact_id = str(uuid4())
                db.execute(
                    """INSERT INTO memory_facts(
                        id, fact_key, display_key, category, content, confidence,
                        source_id, created, updated, expires_at, active
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,1)""",
                    (fact_id, fact_key, display_key, cat, clean_content, float(confidence),
                     source_id, stamp, stamp, expires_at),
                )
            db.execute(
                "INSERT OR REPLACE INTO memory_vectors(fact_id, dim, model, vector) VALUES(?,?,?,?)",
                (fact_id, len(vec), model_name, blob),
            )
        if sync_markdown:
            self._mirror_to_markdown(display_key, clean_content)
        return fact_id

    def _mirror_to_markdown(self, key, content):
        try:
            snapshot = self.files.read("memory")
            self.files.learn([(key[:35], content[:220])], snapshot["revision"])
        except (OSError, ValueError):
            pass

    def sync_from_markdown(self):
        """Ingest human-edited MEMORY.md entries into the structured active memory store."""
        try:
            markdown = self.files.read("memory")["markdown"]
        except (OSError, ValueError):
            return 0
        imported = 0
        current_section = ""
        for raw_line in markdown.splitlines():
            line = raw_line.strip()
            if line.startswith("## "):
                current_section = line[3:].strip()
                continue
            if not line.startswith("- "):
                continue
            if match := FACT.fullmatch(line):
                key, text = match[1].strip(), match[2].strip()
                category = "fact" if current_section == LEARNED[3:] else "note"
            else:
                body = line[2:].strip()
                if not body:
                    continue
                key = excerpt(body, 36).rstrip("…")
                text = body
                category = "preference" if "prefer" in current_section.casefold() else "note"
            if self.upsert(key, text, category=category, source_id="memory.md", sync_markdown=False):
                imported += 1
        return imported

    def count(self):
        with self.store.connect() as db:
            row = db.execute("SELECT count(*) FROM memory_facts WHERE active=1").fetchone()
            return row[0] if row else 0

    def recall(self, query, *, limit=6, include_now=False):
        """Hybrid FTS5 + cosine similarity retrieval over structured active memories."""
        anchor = _stamp()
        with self.store.connect() as db:
            rows = db.execute(
                """SELECT f.id, f.display_key, f.category, f.content, f.updated, f.confidence,
                          v.dim, v.model, v.vector
                   FROM memory_facts f
                   LEFT JOIN memory_vectors v ON v.fact_id=f.id
                   WHERE f.active=1 AND (f.expires_at IS NULL OR f.expires_at>?)
                   ORDER BY f.updated DESC LIMIT 600""",
                (anchor,),
            ).fetchall()
            fts_scores = {}
            if expr := _fts_query(query):
                try:
                    for rank_idx, hit in enumerate(db.execute(
                        """SELECT f.id FROM memory_facts_fts
                           JOIN memory_facts f ON f.rowid=memory_facts_fts.rowid
                           WHERE memory_facts_fts MATCH ? AND f.active=1
                           ORDER BY bm25(memory_facts_fts) LIMIT 24""",
                        (expr,),
                    )):
                        fts_scores[hit["id"]] = 1.0 / (1.0 + 0.15 * rank_idx)
                except sqlite3.OperationalError:
                    pass

        if not rows:
            results = []
        else:
            query_model, query_vec = self._vector_for(query or "")
            hash_vec = query_vec if query_model == HASH_MODEL else bilingual_hash_vector(query or "")
            scored = []
            for idx, row in enumerate(rows):
                lexical = fts_scores.get(row["id"], 0.0)
                semantic = 0.0
                if row["vector"]:
                    stored = unpack_vector(row["vector"])
                    if row["model"] == query_model and len(stored) == len(query_vec):
                        semantic = max(0.0, dot_similarity(query_vec, stored))
                    elif row["model"] == HASH_MODEL and len(stored) == len(hash_vec):
                        semantic = max(0.0, dot_similarity(hash_vec, stored))
                recency = 0.12 / (1.0 + 0.08 * idx)
                score = (0.55 * lexical) + (0.45 * semantic) + recency
                if query.strip() and lexical == 0.0 and semantic < 0.08:
                    continue
                scored.append((score, row))
            scored.sort(key=lambda item: item[0], reverse=True)
            results = [
                {"key": row["display_key"], "category": row["category"], "content": row["content"],
                 "updated": row["updated"]}
                for _, row in scored[:limit]
            ]

        if not include_now:
            return results
        now_focus = ""
        try:
            with self.files.locked():
                now_doc = self.files.current(datetime.now(UTC))["markdown"].strip()
            if now_doc != "# Now":
                now_focus = excerpt(now_doc, 650)
        except (OSError, ValueError):
            pass
        return {"facts": results, "now": now_focus}

    def context_bundle(self, query, *, limit=5):
        """Compact active-memory snapshot ready for automatic turn or goal injection."""
        if self.count() == 0:
            self.sync_from_markdown()
        bundle = self.recall(query, limit=limit, include_now=True)
        if not bundle["facts"] and not bundle["now"]:
            return None
        return bundle
