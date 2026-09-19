"""SQLite schema for the Islamic tutor: learner progress plus a local cache
of verified source text (Quran, hadith) so lessons work offline once
fetched and the text is never re-typed or generated."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import app_paths

DATA_DIR = app_paths.data_dir("islamic")
DEFAULT_DB_PATH = DATA_DIR / "islamic.db"
SCHEMA_VERSION = 1

SCHEMA = """
-- Learning profiles ------------------------------------------------------
CREATE TABLE IF NOT EXISTS students (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL UNIQUE COLLATE NOCASE,
    role        TEXT NOT NULL CHECK (role IN ('parent', 'child')),
    age         INTEGER,
    created_at  DATETIME NOT NULL,
    last_active DATETIME
);

-- Where each student is on each track (alphabet, tajweed, surah, dua, manners).
CREATE TABLE IF NOT EXISTS cursors (
    student_id  INTEGER NOT NULL REFERENCES students(id) ON DELETE CASCADE,
    track       TEXT NOT NULL,
    position    TEXT NOT NULL,          -- JSON: {"index": 3} or {"surah": 112, "ayah": 2}
    updated_at  DATETIME NOT NULL,
    PRIMARY KEY (student_id, track)
);

CREATE TABLE IF NOT EXISTS lesson_progress (
    student_id   INTEGER NOT NULL REFERENCES students(id) ON DELETE CASCADE,
    lesson_key   TEXT NOT NULL,         -- "alphabet:3", "tajweed:qalqalah", "dua:2", "manners:5"
    track        TEXT NOT NULL,
    status       TEXT NOT NULL CHECK (status IN ('started', 'completed')),
    score        REAL,
    attempts     INTEGER NOT NULL DEFAULT 0,
    updated_at   DATETIME NOT NULL,
    completed_at DATETIME,
    PRIMARY KEY (student_id, lesson_key)
);

-- Per-ayah memorisation with simple spaced repetition.
CREATE TABLE IF NOT EXISTS memorization (
    student_id    INTEGER NOT NULL REFERENCES students(id) ON DELETE CASCADE,
    surah         INTEGER NOT NULL,
    ayah          INTEGER NOT NULL,
    strength      INTEGER NOT NULL DEFAULT 0,   -- 0..6
    last_score    REAL,
    last_reviewed DATETIME,
    next_review   DATETIME,
    PRIMARY KEY (student_id, surah, ayah)
);

-- Letters, words and rules a student keeps getting wrong.
CREATE TABLE IF NOT EXISTS weak_areas (
    student_id  INTEGER NOT NULL REFERENCES students(id) ON DELETE CASCADE,
    area        TEXT NOT NULL,          -- "letter:ح", "word:112:3:يولد", "quiz:letters"
    misses      INTEGER NOT NULL DEFAULT 0,
    hits        INTEGER NOT NULL DEFAULT 0,
    last_seen   DATETIME NOT NULL,
    PRIMARY KEY (student_id, area)
);

CREATE TABLE IF NOT EXISTS activity (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    student_id  INTEGER NOT NULL REFERENCES students(id) ON DELETE CASCADE,
    day         DATE NOT NULL,
    kind        TEXT NOT NULL,          -- lesson | quiz | recitation | plan
    detail      TEXT NOT NULL,
    score       REAL,
    created_at  DATETIME NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_activity_student_day ON activity(student_id, day);

-- Verified-source cache ----------------------------------------------------
CREATE TABLE IF NOT EXISTS quran_surahs (
    number       INTEGER PRIMARY KEY,
    name_ar      TEXT NOT NULL,
    name_en      TEXT NOT NULL,
    meaning      TEXT NOT NULL,
    ayah_count   INTEGER NOT NULL,
    revelation   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS quran_ayahs (
    surah           INTEGER NOT NULL,
    ayah            INTEGER NOT NULL,
    global_number   INTEGER NOT NULL,
    arabic          TEXT NOT NULL,      -- Tanzil Uthmani text, basmala prefix removed
    translation     TEXT NOT NULL,      -- Sahih International
    transliteration TEXT NOT NULL,
    PRIMARY KEY (surah, ayah)
);

CREATE TABLE IF NOT EXISTS hadith_cache (
    collection  TEXT NOT NULL,
    number      INTEGER NOT NULL,
    text_en     TEXT NOT NULL,
    text_ar     TEXT,
    grades      TEXT NOT NULL,          -- JSON list of {name, grade}
    fetched_at  DATETIME NOT NULL,
    PRIMARY KEY (collection, number)
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
    conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
    conn.commit()
    return conn
