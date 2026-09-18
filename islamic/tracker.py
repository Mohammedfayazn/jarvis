"""LearningTracker: profiles and progress for each family member.

Students are separate (parent and child never share progress). Tracks:
alphabet, tajweed, surah (memorisation), dua, manners. Memorisation is per
ayah with light spaced repetition, so revision picks what is due.
All data stays in a local SQLite file.
"""
from __future__ import annotations

import dataclasses
import datetime as _dt
import json
import sqlite3

# Days until the next review, indexed by strength (0..6).
REVIEW_INTERVALS = (0, 1, 2, 4, 7, 14, 30)
MEMORIZED_STRENGTH = 3
PASS_SCORE = 0.85


def now() -> _dt.datetime:
    return _dt.datetime.now().replace(microsecond=0)


def now_iso() -> str:
    return now().isoformat()


def today() -> str:
    return _dt.date.today().isoformat()


@dataclasses.dataclass(frozen=True)
class Student:
    id: int
    name: str
    role: str
    age: int | None
    created_at: str
    last_active: str | None

    @property
    def is_child(self) -> bool:
        return self.role == "child"

    def as_dict(self) -> dict:
        return dataclasses.asdict(self)


class LearningTracker:
    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn

    # -- students --------------------------------------------------------

    def add_student(self, name: str, role: str = "child", age: int | None = None) -> tuple[Student, bool]:
        name = " ".join((name or "").split())
        if not name:
            raise ValueError("a student needs a name")
        role = "child" if (role or "").lower().startswith(("child", "kid", "son", "daughter")) else (
            "parent" if (role or "").lower() in ("parent", "adult", "father", "mother", "abbu", "ammi") else None)
        if role is None:
            role = "child" if age is not None and int(age) < 13 else "parent"
        existing = self.get_student(name)
        if existing:
            if age is not None and existing.age != int(age):
                self._conn.execute("UPDATE students SET age = ? WHERE id = ?", (int(age), existing.id))
                self._conn.commit()
                existing = self.get_student(name)
            return existing, False
        cur = self._conn.execute(
            "INSERT INTO students (name, role, age, created_at) VALUES (?, ?, ?, ?)",
            (name, role, int(age) if age is not None else None, now_iso()),
        )
        self._conn.commit()
        return self._student(cur.lastrowid), True

    def _student(self, student_id: int) -> Student | None:
        row = self._conn.execute("SELECT * FROM students WHERE id = ?", (student_id,)).fetchone()
        return Student(**dict(row)) if row else None

    def get_student(self, name: str) -> Student | None:
        row = self._conn.execute(
            "SELECT * FROM students WHERE name = ?", (" ".join((name or "").split()),)
        ).fetchone()
        return Student(**dict(row)) if row else None

    def students(self) -> list[Student]:
        rows = self._conn.execute(
            "SELECT * FROM students ORDER BY COALESCE(last_active, created_at) DESC"
        ).fetchall()
        return [Student(**dict(r)) for r in rows]

    def touch(self, student: Student) -> None:
        self._conn.execute("UPDATE students SET last_active = ? WHERE id = ?", (now_iso(), student.id))
        self._conn.commit()

    # -- where each student is -------------------------------------------

    def cursor(self, student: Student, track: str) -> dict:
        row = self._conn.execute(
            "SELECT position FROM cursors WHERE student_id = ? AND track = ?", (student.id, track)
        ).fetchone()
        return json.loads(row["position"]) if row else {}

    def set_cursor(self, student: Student, track: str, **position) -> None:
        self._conn.execute(
            "INSERT INTO cursors (student_id, track, position, updated_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(student_id, track) DO UPDATE SET position = excluded.position, "
            "updated_at = excluded.updated_at",
            # microseconds: "continue yesterday's lesson" picks the latest
            # track, and two lessons can start within the same second
            (student.id, track, json.dumps(position), _dt.datetime.now().isoformat()),
        )
        self._conn.commit()

    def last_track(self, student: Student) -> tuple[str, dict] | None:
        """The lesson the student was on most recently ("continue yesterday's lesson")."""
        row = self._conn.execute(
            "SELECT track, position FROM cursors WHERE student_id = ? ORDER BY updated_at DESC LIMIT 1",
            (student.id,),
        ).fetchone()
        return (row["track"], json.loads(row["position"])) if row else None

    # -- lessons ---------------------------------------------------------

    def record_lesson(self, student: Student, lesson_key: str, completed: bool,
                      score: float | None = None) -> None:
        track = lesson_key.split(":", 1)[0]
        ts = now_iso()
        self._conn.execute(
            "INSERT INTO lesson_progress (student_id, lesson_key, track, status, score, attempts, "
            "updated_at, completed_at) VALUES (?, ?, ?, ?, ?, 1, ?, ?) "
            "ON CONFLICT(student_id, lesson_key) DO UPDATE SET "
            "status = CASE WHEN lesson_progress.status = 'completed' THEN 'completed' ELSE excluded.status END, "
            "score = COALESCE(excluded.score, lesson_progress.score), "
            "attempts = lesson_progress.attempts + 1, updated_at = excluded.updated_at, "
            "completed_at = COALESCE(lesson_progress.completed_at, excluded.completed_at)",
            (student.id, lesson_key, track, "completed" if completed else "started", score,
             ts, ts if completed else None),
        )
        self._conn.commit()

    def completed_lessons(self, student: Student, track: str | None = None) -> list[str]:
        sql = "SELECT lesson_key FROM lesson_progress WHERE student_id = ? AND status = 'completed'"
        args: list = [student.id]
        if track:
            sql += " AND track = ?"
            args.append(track)
        return [r["lesson_key"] for r in self._conn.execute(sql + " ORDER BY completed_at", args)]

    # -- memorisation ----------------------------------------------------

    def record_recitation(self, student: Student, surah: int, ayah_scores: dict[int, float]) -> None:
        """Spaced repetition per ayah: pass -> stronger and reviewed later;
        fail -> weaker and reviewed tomorrow."""
        ts = now()
        for ayah, score in ayah_scores.items():
            row = self._conn.execute(
                "SELECT strength FROM memorization WHERE student_id = ? AND surah = ? AND ayah = ?",
                (student.id, surah, ayah),
            ).fetchone()
            strength = row["strength"] if row else 0
            strength = min(6, strength + 1) if score >= PASS_SCORE else max(0, strength - 2)
            nxt = ts + _dt.timedelta(days=max(1, REVIEW_INTERVALS[strength]))
            self._conn.execute(
                "INSERT INTO memorization VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(student_id, surah, ayah) DO UPDATE SET strength = excluded.strength, "
                "last_score = excluded.last_score, last_reviewed = excluded.last_reviewed, "
                "next_review = excluded.next_review",
                (student.id, surah, ayah, strength, score, ts.isoformat(), nxt.isoformat()),
            )
        self._conn.commit()

    def learning(self, student: Student, surah: int, ayah: int) -> None:
        """Mark an ayah as started (strength 0) without a test result."""
        self._conn.execute(
            "INSERT OR IGNORE INTO memorization (student_id, surah, ayah, strength, next_review) "
            "VALUES (?, ?, ?, 0, ?)", (student.id, surah, ayah, now_iso()))
        self._conn.commit()

    def memorized_surahs(self, student: Student, surah_lengths: dict[int, int]) -> list[int]:
        rows = self._conn.execute(
            "SELECT surah, COUNT(*) AS n FROM memorization WHERE student_id = ? AND strength >= ? "
            "GROUP BY surah", (student.id, MEMORIZED_STRENGTH),
        ).fetchall()
        return [r["surah"] for r in rows if r["n"] >= surah_lengths.get(r["surah"], 10 ** 6)]

    def memorization_rows(self, student: Student, surah: int | None = None) -> list[dict]:
        sql = "SELECT * FROM memorization WHERE student_id = ?"
        args: list = [student.id]
        if surah:
            sql += " AND surah = ?"
            args.append(surah)
        return [dict(r) for r in self._conn.execute(sql + " ORDER BY surah, ayah", args)]

    def due_for_review(self, student: Student, limit: int = 5) -> list[dict]:
        rows = self._conn.execute(
            "SELECT surah, ayah, strength FROM memorization WHERE student_id = ? AND strength > 0 "
            "AND next_review <= ? ORDER BY strength, next_review LIMIT ?",
            (student.id, now_iso(), limit),
        ).fetchall()
        return [dict(r) for r in rows]

    # -- weak areas ------------------------------------------------------

    def note_area(self, student: Student, area: str, correct: bool) -> None:
        self._conn.execute(
            "INSERT INTO weak_areas VALUES (?, ?, ?, ?, ?) ON CONFLICT(student_id, area) DO UPDATE SET "
            "misses = weak_areas.misses + excluded.misses, hits = weak_areas.hits + excluded.hits, "
            "last_seen = excluded.last_seen",
            (student.id, area, 0 if correct else 1, 1 if correct else 0, now_iso()),
        )
        self._conn.commit()

    def weak_areas(self, student: Student, limit: int = 5) -> list[dict]:
        """Areas missed more often than got right, worst first."""
        rows = self._conn.execute(
            "SELECT area, misses, hits FROM weak_areas WHERE student_id = ? AND misses > hits "
            "ORDER BY misses - hits DESC, last_seen DESC LIMIT ?",
            (student.id, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    # -- activity ----------------------------------------------------------

    def log(self, student: Student, kind: str, detail: str, score: float | None = None) -> None:
        self._conn.execute(
            "INSERT INTO activity (student_id, day, kind, detail, score, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)", (student.id, today(), kind, detail, score, now_iso()))
        self._conn.commit()
        self.touch(student)

    def activity(self, student: Student, days: int = 1) -> list[dict]:
        since = (_dt.date.today() - _dt.timedelta(days=days - 1)).isoformat()
        rows = self._conn.execute(
            "SELECT day, kind, detail, score FROM activity WHERE student_id = ? AND day >= ? "
            "ORDER BY created_at", (student.id, since),
        ).fetchall()
        return [dict(r) for r in rows]

    def streak(self, student: Student) -> int:
        """Consecutive days (ending today or yesterday) with any activity."""
        days = {r["day"] for r in self._conn.execute(
            "SELECT DISTINCT day FROM activity WHERE student_id = ?", (student.id,))}
        day = _dt.date.today()
        if day.isoformat() not in days:
            day -= _dt.timedelta(days=1)
        count = 0
        while day.isoformat() in days:
            count += 1
            day -= _dt.timedelta(days=1)
        return count
