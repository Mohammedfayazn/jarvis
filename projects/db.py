"""SQLite database layer for the project intelligence system.

Schema and connection only. The repositories (brain.py, tasks.py) own the
queries for their own tables; nothing above them writes SQL.
"""
from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

import app_paths

logger = logging.getLogger("jarvis.projects.db")

DATA_DIR = app_paths.data_dir("projects")
DEFAULT_DB_PATH = DATA_DIR / "projects.db"
SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL UNIQUE COLLATE NOCASE,
    path            TEXT NOT NULL UNIQUE,
    description     TEXT,
    current_status  TEXT,
    index_json      TEXT,
    last_indexed_at DATETIME,
    last_opened_at  DATETIME,
    last_worked_on  DATETIME,
    created_at      DATETIME NOT NULL,
    updated_at      DATETIME NOT NULL
);

CREATE TABLE IF NOT EXISTS tasks (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id   INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    title        TEXT NOT NULL,
    description  TEXT,
    status       TEXT NOT NULL DEFAULT 'Todo'
                 CHECK (status IN ('Todo', 'In Progress', 'Blocked', 'Done')),
    priority     TEXT NOT NULL DEFAULT 'Medium'
                 CHECK (priority IN ('Low', 'Medium', 'High')),
    created_at   DATETIME NOT NULL,
    updated_at   DATETIME NOT NULL,
    completed_at DATETIME
);
CREATE INDEX IF NOT EXISTS idx_tasks_project_status ON tasks(project_id, status);

-- Goals, decisions, notes, blockers, next steps, recurring problems and
-- coding preferences. project_id NULL = applies to every project.
CREATE TABLE IF NOT EXISTS entries (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id  INTEGER REFERENCES projects(id) ON DELETE CASCADE,
    kind        TEXT NOT NULL CHECK (kind IN ('goal', 'decision', 'note', 'blocker',
                                              'next_step', 'problem', 'preference')),
    text        TEXT NOT NULL,
    resolved    INTEGER NOT NULL DEFAULT 0,
    created_at  DATETIME NOT NULL,
    resolved_at DATETIME
);
CREATE INDEX IF NOT EXISTS idx_entries_project_kind ON entries(project_id, kind, resolved);

CREATE TABLE IF NOT EXISTS work_sessions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id   INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    started_at   DATETIME NOT NULL,
    ended_at     DATETIME,
    accomplished TEXT,
    next_steps   TEXT
);
CREATE INDEX IF NOT EXISTS idx_sessions_project ON work_sessions(project_id, started_at);
"""


def connect(db_path: Path | str = DEFAULT_DB_PATH) -> sqlite3.Connection:
    """Open the database, creating the schema on first use.

    `check_same_thread=False`: Jarvis calls tools through asyncio.to_thread,
    so the connection is used from worker threads. Calls are serialised by
    the tool dispatcher, never concurrent.
    """
    path = Path(db_path)
    if str(path) != ":memory:":
        path.parent.mkdir(parents=True, exist_ok=True)
        app_paths.warn_if_redirected(path)
    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    if str(path) != ":memory:":
        conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
    conn.commit()
    logger.debug("projects db ready at %s", path)
    return conn
