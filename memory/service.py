"""Service layer: MemoryManager, the one class the rest of Jarvis talks to.

Combines the raw SQL in db.py with fuzzy matching (difflib - the same
approach window_manager.py uses for window titles) and optional semantic
search (embeddings.py). No SQL and no natural-language parsing lives here;
see db.py and intent.py respectively.
"""
from __future__ import annotations

import difflib
import logging
import re
import sqlite3
from pathlib import Path

from . import db as db_layer
from . import embeddings
from .models import Memory, normalize_category, now_iso

logger = logging.getLogger("jarvis.memory.service")

# High on purpose: 'daughter_age' vs 'daughter_name' scores 0.88 and must NOT
# match, since forget_memory would then delete the wrong fact. Real typos
# ('favorit_editor') score ~0.97.
KEY_MATCH_THRESHOLD = 0.9
SEMANTIC_MATCH_THRESHOLD = 0.45

_FILLER_WORDS = {
    "my", "the", "a", "an", "is", "are", "what", "when", "where", "does",
    "do", "did", "i", "time", "for", "please", "of", "to", "me", "you",
}


def _slugify_key(key: str) -> str:
    """Turn a spoken phrase into a stable lookup key.

    "Daughter's School" -> "daughters_school", so get/update/delete work
    the same regardless of how the caller capitalised or punctuated it.
    """
    key = key.strip().lower()
    key = re.sub(r"[\u2019']", "", key)
    key = re.sub(r"[^a-z0-9]+", "_", key).strip("_")
    return key or "memory"


def _keywords(text: str) -> set[str]:
    words = re.findall(r"[a-z0-9]+", text.lower())
    out = set()
    for w in words:
        if w in _FILLER_WORDS or len(w) <= 1:
            continue
        if len(w) > 3 and w.endswith("s"):
            w = w[:-1]
        out.add(w)
    return out


class MemoryManager:
    """Jarvis's long-term memory: SQLite-backed key/value facts with fuzzy
    and (optionally) semantic recall on top."""

    def __init__(self, db_path: Path | str | None = None):
        self._conn: sqlite3.Connection = db_layer.connect(
            db_path or db_layer.DEFAULT_DB_PATH
        )

    def close(self) -> None:
        self._conn.close()

    # -- writes --------------------------------------------------------

    def add_memory(self, category: str, key: str, value: str) -> Memory:
        """Store a fact. Storing the same key again updates it in place -
        "remember X" said twice should never error, just correct itself."""
        slug = _slugify_key(key)
        category = normalize_category(category)
        ts = now_iso()
        memory_id, _created = db_layer.upsert_memory(
            self._conn, category, slug, value.strip(), ts, ts
        )
        self._index_embedding(memory_id, slug, value)
        row = db_layer.get_by_id(self._conn, memory_id)
        return Memory.from_row(row)

    def update_memory(self, key: str, value: str) -> Memory | None:
        """Change the value of an already-remembered fact. Returns None if
        nothing matching `key` (exact or fuzzy) exists yet."""
        slug = self._resolve_key(key)
        if slug is None:
            return None
        ts = now_iso()
        db_layer.update_memory_value(self._conn, slug, value.strip(), ts)
        row = db_layer.get_by_key(self._conn, slug)
        self._index_embedding(row["id"], slug, value)
        return Memory.from_row(row)

    def delete_memory(self, key: str) -> bool:
        slug = self._resolve_key(key)
        if slug is None:
            return False
        row = db_layer.get_by_key(self._conn, slug)
        if row is not None:
            db_layer.delete_embedding(self._conn, row["id"])
        return db_layer.delete_by_key(self._conn, slug) > 0

    # -- reads -----------------------------------------------------------

    def has_exact(self, key: str) -> bool:
        return db_layer.get_by_key(self._conn, _slugify_key(key)) is not None

    def get_memory(self, key: str) -> Memory | None:
        slug = self._resolve_key(key)
        if slug is None:
            return None
        row = db_layer.get_by_key(self._conn, slug)
        return Memory.from_row(row) if row else None

    def list_memories(self, category: str | None = None) -> list[Memory]:
        cat = normalize_category(category) if category else None
        return [Memory.from_row(r) for r in db_layer.list_all(self._conn, cat)]

    def search_memory(self, query: str, limit: int = 3) -> list[tuple[Memory, float]]:
        """Best-effort recall for a free-form question.

        Combines exact/fuzzy key matching, keyword overlap over key+value,
        and (if available) semantic similarity. Returns (memory, score)
        pairs sorted best-first; score is in [0, 1] and not comparable
        across queries, only within one call's results.
        """
        results: dict[int, float] = {}
        memories: dict[int, Memory] = {}

        def _consider(row, score) -> None:
            m = Memory.from_row(row)
            if score > results.get(m.id, -1.0):
                results[m.id] = score
                memories[m.id] = m

        slug = self._resolve_key(query)
        if slug:
            row = db_layer.get_by_key(self._conn, slug)
            if row:
                _consider(row, 1.0)

        q_words = _keywords(query)
        if q_words:
            for row in db_layer.list_all(self._conn):
                row_words = _keywords(f"{row['key']} {row['value']}")
                if not row_words:
                    continue
                overlap = len(q_words & row_words) / len(q_words)
                if overlap > 0:
                    _consider(row, 0.5 * overlap)

        if embeddings.available():
            q_vec = embeddings.embed(query)
            if q_vec is not None:
                for row in db_layer.all_embeddings(self._conn):
                    vec = embeddings.from_bytes(row["vector"])
                    sim = embeddings.cosine(q_vec, vec)
                    if sim >= SEMANTIC_MATCH_THRESHOLD:
                        _consider(row, sim)

        ranked = sorted(results.items(), key=lambda kv: kv[1], reverse=True)
        return [(memories[mid], score) for mid, score in ranked[:limit]]

    # -- internals ---------------------------------------------------------

    def _resolve_key(self, text: str, threshold: float = KEY_MATCH_THRESHOLD) -> str | None:
        slug = _slugify_key(text)
        if db_layer.get_by_key(self._conn, slug):
            return slug
        keys = db_layer.all_keys(self._conn)
        if not keys:
            return None
        match = difflib.get_close_matches(slug, keys, n=1, cutoff=threshold)
        return match[0] if match else None

    def _index_embedding(self, memory_id: int, key: str, value: str) -> None:
        if not embeddings.available():
            return
        vector = embeddings.embed(f"{key.replace('_', ' ')}: {value}")
        if vector is not None:
            db_layer.set_embedding(
                self._conn, memory_id, embeddings.MODEL_NAME,
                embeddings.to_bytes(vector),
            )
