"""ProjectBrain: each project's long-term memory.

Owns the projects, entries and work_sessions tables, and composes
TaskManager for tasks. Everything here is plain storage and lookup - no
git, no filesystem scanning, no wording of answers.
"""
from __future__ import annotations

import datetime as _dt
import difflib
import json
import re
import sqlite3
from pathlib import Path

from .models import (
    ENTRY_KINDS, Entry, Project, ProjectMemory, TaskStatus, WorkSession,
    normalize_kind, now, now_iso, parse_time,
)
from .tasks import TaskManager

NAME_CUTOFF = 0.8
# A session left open longer than this (Jarvis never heard "done for today")
# is closed quietly when the project is opened again.
STALE_SESSION = _dt.timedelta(hours=12)
_FILLER = {"project", "projects", "the", "my", "repo", "folder", "app", "open"}


def normalize_name(text: str) -> str:
    words = re.findall(r"[a-z0-9]+", (text or "").lower())
    return "".join(w for w in words if w not in _FILLER)


class ProjectBrain:
    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn
        self.tasks = TaskManager(conn)

    # -- projects --------------------------------------------------------

    def get(self, project_id: int) -> Project | None:
        row = self._conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
        return Project.from_row(row) if row else None

    def get_by_path(self, path) -> Project | None:
        row = self._conn.execute(
            "SELECT * FROM projects WHERE path = ?", (str(Path(path).resolve()),)
        ).fetchone()
        return Project.from_row(row) if row else None

    def list_projects(self) -> list[Project]:
        rows = self._conn.execute(
            "SELECT * FROM projects ORDER BY COALESCE(last_opened_at, created_at) DESC"
        ).fetchall()
        return [Project.from_row(r) for r in rows]

    def find(self, name: str) -> list[Project]:
        """Registered projects matching a spoken name (best tier only)."""
        wanted = normalize_name(name)
        if not wanted:
            return []
        scored = []
        for project in self.list_projects():
            have = normalize_name(project.name)
            if not have:
                continue
            if have == wanted:
                score = 1.0
            elif len(wanted) >= 3 and (have.startswith(wanted) or wanted.startswith(have)):
                score = 0.9
            else:
                score = difflib.SequenceMatcher(None, wanted, have).ratio()
            if score >= NAME_CUTOFF:
                scored.append((score, project))
        if not scored:
            return []
        best = max(s for s, _ in scored)
        return [p for s, p in scored if s >= best - 0.02]

    def register(self, name: str, path, description: str | None = None) -> Project:
        """Add a project, or return the existing one for this folder."""
        path = str(Path(path).resolve())
        existing = self.get_by_path(path)
        if existing:
            return existing
        if name.islower():
            name = name[:1].upper() + name[1:]     # "homemade" folder -> "Homemade"
        base, n = name, 2
        while self._conn.execute(
            "SELECT 1 FROM projects WHERE name = ?", (name,)
        ).fetchone():
            # two folders with the same name: "api", "api (2)"
            name = f"{base} ({n})"
            n += 1
        ts = now_iso()
        cur = self._conn.execute(
            "INSERT INTO projects (name, path, description, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (name, path, description, ts, ts),
        )
        self._conn.commit()
        return self.get(cur.lastrowid)

    def rename(self, project_id: int, name: str) -> Project:
        self._update(project_id, name=name)
        return self.get(project_id)

    def last_opened(self) -> Project | None:
        row = self._conn.execute(
            "SELECT * FROM projects WHERE last_opened_at IS NOT NULL "
            "ORDER BY last_opened_at DESC LIMIT 1"
        ).fetchone()
        return Project.from_row(row) if row else None

    def mark_opened(self, project_id: int) -> None:
        self._update(project_id, last_opened_at=now_iso())

    def mark_worked(self, project_id: int, when: str | None = None) -> None:
        self._update(project_id, last_worked_on=when or now_iso())

    def save_index(self, project_id: int, index: dict, description: str | None) -> Project:
        fields = {"index_json": json.dumps(index), "last_indexed_at": now_iso()}
        current = self.get(project_id)
        # Never overwrite a description the user gave with a guessed one.
        if description and not (current and current.description):
            fields["description"] = description
        self._update(project_id, **fields)
        return self.get(project_id)

    def set_description(self, project_id: int, description: str) -> None:
        self._update(project_id, description=description)

    def set_status(self, project_id: int, status: str) -> None:
        self._update(project_id, current_status=status)

    def _update(self, project_id: int, **fields) -> None:
        fields["updated_at"] = now_iso()
        cols = ", ".join(f"{k} = ?" for k in fields)
        self._conn.execute(
            f"UPDATE projects SET {cols} WHERE id = ?", [*fields.values(), project_id]
        )
        self._conn.commit()

    # -- entries: goals, decisions, notes, blockers, next steps ... --------

    def add_entry(self, project_id: int | None, kind: str, text: str) -> Entry:
        kind = normalize_kind(kind)
        if kind not in ENTRY_KINDS:
            raise ValueError(f"unknown kind; use one of {', '.join(ENTRY_KINDS)}")
        text = " ".join((text or "").split())
        if not text:
            raise ValueError("entry text is empty")
        for entry in self.entries(project_id, [kind], include_global=False):
            if entry.text.lower() == text.lower():
                return entry
        cur = self._conn.execute(
            "INSERT INTO entries (project_id, kind, text, created_at) VALUES (?, ?, ?, ?)",
            (project_id, kind, text, now_iso()),
        )
        self._conn.commit()
        return self._entry(cur.lastrowid)

    def _entry(self, entry_id: int) -> Entry | None:
        row = self._conn.execute("SELECT * FROM entries WHERE id = ?", (entry_id,)).fetchone()
        return Entry.from_row(row) if row else None

    def entries(self, project_id: int | None, kinds=None, *, include_resolved=False,
                include_global=True, limit: int | None = None,
                since: str | None = None) -> list[Entry]:
        where, args = [], []
        if project_id is None:
            where.append("project_id IS NULL")
        elif include_global:
            where.append("(project_id = ? OR project_id IS NULL)")
            args.append(project_id)
        else:
            where.append("project_id = ?")
            args.append(project_id)
        if kinds:
            kinds = [normalize_kind(k) for k in kinds]
            where.append(f"kind IN ({', '.join('?' * len(kinds))})")
            args.extend(kinds)
        if not include_resolved:
            where.append("resolved = 0")
        if since:
            where.append("created_at >= ?")
            args.append(since)
        sql = f"SELECT * FROM entries WHERE {' AND '.join(where)} ORDER BY created_at DESC, id DESC"
        if limit:
            sql += f" LIMIT {int(limit)}"
        return [Entry.from_row(r) for r in self._conn.execute(sql, args).fetchall()]

    def find_entries(self, project_id: int | None, query: str, kinds=None) -> list[Entry]:
        """Unresolved entries whose text matches `query` (keyword overlap)."""
        words = set(re.findall(r"[a-z0-9]+", (query or "").lower())) - {"the", "a", "an", "is", "on"}
        if not words:
            return []
        scored = []
        for entry in self.entries(project_id, kinds):
            have = set(re.findall(r"[a-z0-9]+", entry.text.lower()))
            overlap = len(words & have) / len(words)
            if overlap >= 0.5:
                scored.append((overlap, entry))
        if not scored:
            return []
        best = max(s for s, _ in scored)
        return [e for s, e in scored if s >= best - 0.01]

    def resolve_entry(self, entry_id: int) -> Entry:
        self._conn.execute(
            "UPDATE entries SET resolved = 1, resolved_at = ? WHERE id = ?",
            (now_iso(), entry_id),
        )
        self._conn.commit()
        return self._entry(entry_id)

    # -- work sessions ---------------------------------------------------

    def open_session(self, project_id: int) -> WorkSession | None:
        row = self._conn.execute(
            "SELECT * FROM work_sessions WHERE project_id = ? AND ended_at IS NULL "
            "ORDER BY started_at DESC LIMIT 1",
            (project_id,),
        ).fetchone()
        return WorkSession.from_row(row) if row else None

    def start_session(self, project_id: int) -> WorkSession:
        current = self.open_session(project_id)
        if current:
            started = parse_time(current.started_at)
            if started and now() - started < STALE_SESSION:
                return current
            # Forgotten session: close it where it began, with no summary,
            # so it never counts as hours of work that did not happen.
            self._conn.execute(
                "UPDATE work_sessions SET ended_at = started_at WHERE id = ?", (current.id,)
            )
        cur = self._conn.execute(
            "INSERT INTO work_sessions (project_id, started_at) VALUES (?, ?)",
            (project_id, now_iso()),
        )
        self._conn.commit()
        return self._session(cur.lastrowid)

    def end_session(self, project_id: int, accomplished: str | None,
                    next_steps: str | None) -> WorkSession:
        """Close today's session with what was done and what comes next.

        The answer to "what should be done next?" replaces earlier planned
        next steps - old plans are marked resolved, not deleted.
        """
        accomplished = " ".join((accomplished or "").split()) or None
        next_steps = " ".join((next_steps or "").split()) or None
        current = self.open_session(project_id) or self.start_session(project_id)
        ts = now_iso()
        self._conn.execute(
            "UPDATE work_sessions SET ended_at = ?, accomplished = ?, next_steps = ? "
            "WHERE id = ?",
            (ts, accomplished, next_steps, current.id),
        )
        if next_steps:
            self._conn.execute(
                "UPDATE entries SET resolved = 1, resolved_at = ? "
                "WHERE project_id = ? AND kind = 'next_step' AND resolved = 0",
                (ts, project_id),
            )
            self._conn.execute(
                "INSERT INTO entries (project_id, kind, text, created_at) "
                "VALUES (?, 'next_step', ?, ?)",
                (project_id, next_steps, ts),
            )
        self._conn.commit()
        if accomplished:
            self.set_status(project_id, accomplished)
        self.mark_worked(project_id, ts)
        return self._session(current.id)

    def _session(self, session_id: int) -> WorkSession | None:
        row = self._conn.execute(
            "SELECT * FROM work_sessions WHERE id = ?", (session_id,)
        ).fetchone()
        return WorkSession.from_row(row) if row else None

    def last_session(self, project_id: int) -> WorkSession | None:
        """Most recent session the user actually summarised."""
        row = self._conn.execute(
            "SELECT * FROM work_sessions WHERE project_id = ? AND ended_at IS NOT NULL "
            "AND (accomplished IS NOT NULL OR next_steps IS NOT NULL) "
            "ORDER BY ended_at DESC LIMIT 1",
            (project_id,),
        ).fetchone()
        return WorkSession.from_row(row) if row else None

    def sessions_since(self, project_id: int | None, since_iso: str) -> list[WorkSession]:
        sql = ("SELECT * FROM work_sessions WHERE ended_at >= ? "
               "AND (accomplished IS NOT NULL OR next_steps IS NOT NULL)")
        args = [since_iso]
        if project_id is not None:
            sql += " AND project_id = ?"
            args.append(project_id)
        rows = self._conn.execute(sql + " ORDER BY ended_at DESC", args).fetchall()
        return [WorkSession.from_row(r) for r in rows]

    # -- the whole picture -------------------------------------------------

    def memory(self, project: Project) -> ProjectMemory:
        texts = lambda kind, limit=10: [
            e.text for e in self.entries(project.id, [kind], include_global=False, limit=limit)
        ]
        blocked_tasks = [t.title for t in self.tasks.list(project.id, [TaskStatus.BLOCKED])]
        return ProjectMemory(
            project_name=project.name,
            project_path=project.path,
            project_description=project.description,
            goals=texts("goal"),
            current_status=project.current_status,
            completed_tasks=self.tasks.list(project.id, [TaskStatus.DONE], limit=10),
            pending_tasks=self.tasks.open_tasks(project.id),
            blockers=texts("blocker") + blocked_tasks,
            decisions=texts("decision"),
            notes=texts("note"),
            last_worked_on=project.last_worked_on,
            next_steps=texts("next_step", 3),
        )
