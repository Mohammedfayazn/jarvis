"""ProgressTracker: what each child has practised, stored locally."""
from __future__ import annotations

import dataclasses
import datetime as _dt
import json
import sqlite3

LEARNED_AFTER = 2          # heard correctly this many times -> "learned"


def now_iso() -> str:
    return _dt.datetime.now().replace(microsecond=0).isoformat()


def today() -> str:
    return _dt.date.today().isoformat()


def days_ago(n: int) -> str:
    return (_dt.date.today() - _dt.timedelta(days=n)).isoformat()


@dataclasses.dataclass(frozen=True)
class Child:
    id: int
    name: str
    age: int | None
    languages: tuple[str, ...]
    focus_language: str
    stars: int

    @classmethod
    def from_row(cls, row) -> "Child":
        return cls(row["id"], row["name"], row["age"], tuple(json.loads(row["languages"])),
                   row["focus_language"], row["stars"])

    def as_dict(self) -> dict:
        data = dataclasses.asdict(self)
        data["languages"] = list(self.languages)
        return data


def match_name(spoken: str, stored: str) -> bool:
    """Does a spoken name mean this stored name?

    Case doesn't matter ("zunaira" is "Zunaira"), and a first name finds the
    full one ("Zunaira" -> "Zunaira Fatima"): Jarvis lowercases what it
    heard, and people say one name where the profile has two. An exact
    match is preferred by the callers below, so a real "Ali" is never
    mistaken for "Ali Hassan" when both exist.
    """
    said = " ".join((spoken or "").casefold().split())
    have = " ".join((stored or "").casefold().split())
    if not said or not have:
        return False
    if said == have:
        return True
    have_words = have.split()
    return all(word in have_words for word in said.split())


class ProgressTracker:
    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn

    # -- children ----------------------------------------------------------

    def add_child(self, name: str, age: int | None, languages, focus: str) -> tuple[Child, bool]:
        name = " ".join((name or "").split())
        existing = self.get_child(name)
        if existing:
            self._conn.execute(
                "UPDATE children SET age = COALESCE(?, age), languages = ?, focus_language = ? "
                "WHERE id = ?", (age, json.dumps(list(languages)), focus, existing.id))
            self._conn.commit()
            return self.get_child(name), False
        cur = self._conn.execute(
            "INSERT INTO children (name, age, languages, focus_language, created_at) "
            "VALUES (?, ?, ?, ?, ?)", (name, age, json.dumps(list(languages)), focus, now_iso()))
        self._conn.commit()
        return self._child(cur.lastrowid), True

    def _child(self, child_id: int) -> Child | None:
        row = self._conn.execute("SELECT * FROM children WHERE id = ?", (child_id,)).fetchone()
        return Child.from_row(row) if row else None

    def get_child(self, name: str) -> Child | None:
        """The one child that name means, or None (none, or more than one)."""
        found = self.find_children(name)
        return found[0] if len(found) == 1 else None

    def find_children(self, name: str) -> list[Child]:
        """Every child a spoken name could mean - exact match wins."""
        children = self.children()
        exact = [c for c in children if match_name(name, c.name)
                 and " ".join((name or "").casefold().split())
                 == " ".join(c.name.casefold().split())]
        return exact or [c for c in children if match_name(name, c.name)]

    def children(self) -> list[Child]:
        return [Child.from_row(r) for r in self._conn.execute("SELECT * FROM children ORDER BY id")]

    def set_focus(self, child: Child, language: str) -> Child:
        self._conn.execute("UPDATE children SET focus_language = ? WHERE id = ?", (language, child.id))
        self._conn.commit()
        return self._child(child.id)

    def add_stars(self, child: Child, n: int) -> int:
        self._conn.execute("UPDATE children SET stars = stars + ? WHERE id = ?", (n, child.id))
        self._conn.commit()
        return self._child(child.id).stars

    # -- vocabulary --------------------------------------------------------

    def _touch_word(self, child: Child, key: str, language: str, column: str | None) -> None:
        ts = now_iso()
        self._conn.execute(
            "INSERT INTO word_progress (child_id, word_key, language, first_seen, last_seen) "
            "VALUES (?, ?, ?, ?, ?) ON CONFLICT DO UPDATE SET last_seen = excluded.last_seen",
            (child.id, key, language, ts, ts))
        if column:
            self._conn.execute(
                f"UPDATE word_progress SET {column} = {column} + 1 "
                "WHERE child_id = ? AND word_key = ? AND language = ?", (child.id, key, language))
        self._conn.execute(
            "UPDATE word_progress SET learned_at = ? WHERE child_id = ? AND word_key = ? "
            "AND language = ? AND learned_at IS NULL AND recognized >= ?",
            (ts, child.id, key, language, LEARNED_AFTER))
        self._conn.commit()

    def word_seen(self, child: Child, key: str, language: str) -> None:
        self._touch_word(child, key, language, "seen")

    def word_attempt(self, child: Child, key: str, language: str, recognized: bool) -> None:
        self._touch_word(child, key, language, "practised")
        if recognized:
            self._touch_word(child, key, language, "recognized")

    def words_seen(self, child: Child, language: str) -> set[str]:
        return {r["word_key"] for r in self._conn.execute(
            "SELECT word_key FROM word_progress WHERE child_id = ? AND language = ? AND seen > 0",
            (child.id, language))}

    def words_to_review(self, child: Child, language: str, count: int) -> list[str]:
        """Seen but not learned yet, least recently practised first."""
        return [r["word_key"] for r in self._conn.execute(
            "SELECT word_key FROM word_progress WHERE child_id = ? AND language = ? "
            "AND learned_at IS NULL AND seen > 0 ORDER BY last_seen LIMIT ?",
            (child.id, language, count))]

    def words_learned_since(self, child: Child, since: str) -> list[dict]:
        return [dict(r) for r in self._conn.execute(
            "SELECT word_key, language FROM word_progress WHERE child_id = ? AND learned_at >= ? "
            "ORDER BY learned_at", (child.id, since))]

    def words_practised_since(self, child: Child, since: str) -> int:
        """Word attempts on words practised in the period (attempt counts
        are cumulative per word, so this is an upper bound for the period)."""
        row = self._conn.execute(
            "SELECT COALESCE(SUM(practised), 0) AS n FROM word_progress "
            "WHERE child_id = ? AND last_seen >= ?", (child.id, since)).fetchone()
        return row["n"]

    def words_seen_since(self, child: Child, since: str) -> list[dict]:
        return [dict(r) for r in self._conn.execute(
            "SELECT word_key, language FROM word_progress WHERE child_id = ? AND first_seen >= ?",
            (child.id, since))]

    # -- sounds ------------------------------------------------------------

    def sound_attempt(self, child: Child, language: str, sound: str, success: bool) -> None:
        self._conn.execute(
            "INSERT INTO sound_progress VALUES (?, ?, ?, 1, ?, ?) ON CONFLICT DO UPDATE SET "
            "attempts = attempts + 1, successes = successes + excluded.successes, "
            "last_seen = excluded.last_seen",
            (child.id, language, sound, 1 if success else 0, now_iso()))
        self._conn.commit()

    def sounds(self, child: Child, since: str | None = None) -> list[dict]:
        sql = "SELECT language, sound, attempts, successes FROM sound_progress WHERE child_id = ?"
        args: list = [child.id]
        if since:
            sql += " AND last_seen >= ?"
            args.append(since)
        return [dict(r) for r in self._conn.execute(sql + " ORDER BY attempts DESC", args)]

    def practising_sounds(self, child: Child, min_attempts: int = 3) -> list[dict]:
        """Sounds tried often that are still hard (less than half heard)."""
        return [s for s in self.sounds(child)
                if s["attempts"] >= min_attempts and s["successes"] / s["attempts"] < 0.5]

    # -- conversation ------------------------------------------------------

    def log_utterance(self, child: Child, text: str, words: int, languages: list[str],
                      expanded: str | None = None) -> None:
        self._conn.execute(
            "INSERT INTO utterances (child_id, day, text, words, languages, expanded, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (child.id, today(), text, words, json.dumps(languages), expanded, now_iso()))
        self._conn.commit()

    def utterances(self, child: Child, since: str, until: str | None = None) -> list[dict]:
        sql = "SELECT * FROM utterances WHERE child_id = ? AND day >= ?"
        args: list = [child.id, since]
        if until:
            sql += " AND day < ?"
            args.append(until)
        rows = self._conn.execute(sql + " ORDER BY id", args).fetchall()
        return [{**dict(r), "languages": json.loads(r["languages"])} for r in rows]

    # -- sessions and lessons ---------------------------------------------

    def start_session(self, child: Child, language: str, plan: list[dict]) -> int:
        cur = self._conn.execute(
            "INSERT INTO sessions (child_id, day, language, plan, started_at) VALUES (?, ?, ?, ?, ?)",
            (child.id, today(), language, json.dumps(plan), now_iso()))
        self._conn.commit()
        return cur.lastrowid

    def session(self, session_id: int) -> dict | None:
        row = self._conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
        return {**dict(row), "plan": json.loads(row["plan"])} if row else None

    def advance_session(self, session_id: int) -> None:
        self._conn.execute("UPDATE sessions SET step = step + 1 WHERE id = ?", (session_id,))
        self._conn.commit()

    def end_session(self, session_id: int, stars: int) -> None:
        self._conn.execute("UPDATE sessions SET ended_at = ?, stars = ? WHERE id = ?",
                           (now_iso(), stars, session_id))
        self._conn.commit()

    def sessions(self, child: Child, since: str) -> list[dict]:
        return [dict(r) for r in self._conn.execute(
            "SELECT * FROM sessions WHERE child_id = ? AND day >= ? ORDER BY id", (child.id, since))]

    def complete_lesson(self, child: Child, key: str) -> None:
        self._conn.execute("INSERT OR IGNORE INTO lessons VALUES (?, ?, ?)", (child.id, key, now_iso()))
        self._conn.commit()

    def lessons_since(self, child: Child, since: str) -> list[str]:
        return [r["lesson_key"] for r in self._conn.execute(
            "SELECT lesson_key FROM lessons WHERE child_id = ? AND completed_at >= ?", (child.id, since))]

    def lessons(self, child: Child) -> set[str]:
        return {r["lesson_key"] for r in self._conn.execute(
            "SELECT lesson_key FROM lessons WHERE child_id = ?", (child.id,))}
