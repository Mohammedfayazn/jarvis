"""SQLite schema for the child speech coach. Everything stays on this
computer: what the child said is stored as text for the parent dashboard;
audio is never stored."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import app_paths

DATA_DIR = app_paths.data_dir("speech_coach")
DEFAULT_DB_PATH = DATA_DIR / "speech_coach.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS children (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    name           TEXT NOT NULL UNIQUE COLLATE NOCASE,
    age            INTEGER,
    languages      TEXT NOT NULL,          -- JSON list, e.g. ["nl","en","hi"]
    focus_language TEXT NOT NULL,          -- the one lessons are mainly in
    stars          INTEGER NOT NULL DEFAULT 0,
    created_at     DATETIME NOT NULL
);

CREATE TABLE IF NOT EXISTS word_progress (
    child_id    INTEGER NOT NULL REFERENCES children(id) ON DELETE CASCADE,
    word_key    TEXT NOT NULL,
    language    TEXT NOT NULL,
    seen        INTEGER NOT NULL DEFAULT 0,   -- shown in a lesson
    practised   INTEGER NOT NULL DEFAULT 0,   -- tried saying it
    recognized  INTEGER NOT NULL DEFAULT 0,   -- speech model heard the word
    first_seen  DATETIME NOT NULL,
    last_seen   DATETIME NOT NULL,
    learned_at  DATETIME,
    PRIMARY KEY (child_id, word_key, language)
);

CREATE TABLE IF NOT EXISTS sound_progress (
    child_id   INTEGER NOT NULL REFERENCES children(id) ON DELETE CASCADE,
    language   TEXT NOT NULL,
    sound      TEXT NOT NULL,
    attempts   INTEGER NOT NULL DEFAULT 0,
    successes  INTEGER NOT NULL DEFAULT 0,
    last_seen  DATETIME NOT NULL,
    PRIMARY KEY (child_id, language, sound)
);

-- What the child said in practice conversations, for sentence-length trends.
CREATE TABLE IF NOT EXISTS utterances (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    child_id    INTEGER NOT NULL REFERENCES children(id) ON DELETE CASCADE,
    day         DATE NOT NULL,
    text        TEXT NOT NULL,
    words       INTEGER NOT NULL,
    languages   TEXT NOT NULL,          -- JSON list of languages detected
    expanded    TEXT,                   -- the bigger sentence Jarvis modelled
    created_at  DATETIME NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_utterances_child_day ON utterances(child_id, day);

CREATE TABLE IF NOT EXISTS sessions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    child_id    INTEGER NOT NULL REFERENCES children(id) ON DELETE CASCADE,
    day         DATE NOT NULL,
    language    TEXT NOT NULL,
    plan        TEXT NOT NULL,          -- JSON list of parts
    step        INTEGER NOT NULL DEFAULT 0,
    started_at  DATETIME NOT NULL,
    ended_at    DATETIME,
    stars       INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS lessons (
    child_id     INTEGER NOT NULL REFERENCES children(id) ON DELETE CASCADE,
    lesson_key   TEXT NOT NULL,         -- "vocab:animals:nl", "story:park:en"
    completed_at DATETIME NOT NULL,
    PRIMARY KEY (child_id, lesson_key)
);
"""


def connect(db_path: Path | str = DEFAULT_DB_PATH) -> sqlite3.Connection:
    path = str(db_path)
    if path != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        app_paths.warn_if_redirected(path)
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    if path != ":memory:":
        conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    conn.commit()
    return conn
