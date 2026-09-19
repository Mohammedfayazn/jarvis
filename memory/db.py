"""SQLite database layer for Jarvis's personal memory.

Only raw SQL lives here - no fuzzy matching, no embeddings, no natural
language parsing. `service.py` is the only caller; keeping this layer dumb
means the storage format can change without touching matching logic.
"""
from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

import app_paths

logger = logging.getLogger("jarvis.memory.db")

DEFAULT_DB_PATH = app_paths.data_dir("memory") / "jarvis_memory.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS memories (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    category    TEXT NOT NULL,
    key         TEXT NOT NULL,
    value       TEXT NOT NULL,
    created_at  DATETIME NOT NULL,
    updated_at  DATETIME NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_memories_key ON memories(key);
CREATE INDEX IF NOT EXISTS idx_memories_category ON memories(category);

CREATE TABLE IF NOT EXISTS memory_embeddings (
    memory_id   INTEGER PRIMARY KEY REFERENCES memories(id) ON DELETE CASCADE,
    model       TEXT NOT NULL,
    vector      BLOB NOT NULL
);
"""


def connect(db_path: Path | str = DEFAULT_DB_PATH) -> sqlite3.Connection:
    """Open (and lazily initialise) the memory database.

    WAL mode lets the calling thread read/write without stepping on the
    reconnect logic elsewhere in Jarvis that runs on its own threads.
    `check_same_thread=False` because tool calls run inside
    `asyncio.to_thread`, not on the thread that opened the connection.
    """
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    app_paths.warn_if_redirected(db_path)
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    conn.commit()
    logger.debug("memory db ready at %s", db_path)
    return conn


def insert_memory(conn, category, key, value, created_at, updated_at) -> int:
    cur = conn.execute(
        "INSERT INTO memories (category, key, value, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (category, key, value, created_at, updated_at),
    )
    conn.commit()
    return cur.lastrowid


def upsert_memory(conn, category, key, value, created_at, updated_at) -> tuple[int, bool]:
    """Insert, or update in place if `key` already exists.

    Returns (memory_id, created) so callers can say "yaad rakh liya" vs
    "update kar diya" accurately instead of assuming.
    """
    existing = get_by_key(conn, key)
    if existing is None:
        return insert_memory(conn, category, key, value, created_at, updated_at), True
    conn.execute(
        "UPDATE memories SET category = ?, value = ?, updated_at = ? WHERE key = ?",
        (category, value, updated_at, key),
    )
    conn.commit()
    return existing["id"], False


def update_memory_value(conn, key, value, updated_at) -> int:
    cur = conn.execute(
        "UPDATE memories SET value = ?, updated_at = ? WHERE key = ?",
        (value, updated_at, key),
    )
    conn.commit()
    return cur.rowcount


def get_by_key(conn, key) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM memories WHERE key = ?", (key,)).fetchone()


def get_by_id(conn, memory_id) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM memories WHERE id = ?", (memory_id,)).fetchone()


def delete_by_key(conn, key) -> int:
    cur = conn.execute("DELETE FROM memories WHERE key = ?", (key,))
    conn.commit()
    return cur.rowcount


def list_all(conn, category=None) -> list[sqlite3.Row]:
    if category:
        return conn.execute(
            "SELECT * FROM memories WHERE category = ? ORDER BY updated_at DESC",
            (category,),
        ).fetchall()
    return conn.execute("SELECT * FROM memories ORDER BY updated_at DESC").fetchall()


def all_keys(conn) -> list[str]:
    return [r["key"] for r in conn.execute("SELECT key FROM memories").fetchall()]


def set_embedding(conn, memory_id, model, vector_bytes) -> None:
    conn.execute(
        "INSERT INTO memory_embeddings (memory_id, model, vector) VALUES (?, ?, ?) "
        "ON CONFLICT(memory_id) DO UPDATE SET model = excluded.model, "
        "vector = excluded.vector",
        (memory_id, model, vector_bytes),
    )
    conn.commit()


def all_embeddings(conn) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT m.id, m.key, m.value, m.category, e.vector, e.model "
        "FROM memories m JOIN memory_embeddings e ON e.memory_id = m.id"
    ).fetchall()


def delete_embedding(conn, memory_id) -> None:
    conn.execute("DELETE FROM memory_embeddings WHERE memory_id = ?", (memory_id,))
    conn.commit()
