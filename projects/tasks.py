"""TaskManager: per-project task tracking (Todo / In Progress / Blocked / Done)."""
from __future__ import annotations

import difflib
import re
import sqlite3

from .models import (
    OPEN_STATUSES, STATUS_ORDER, Priority, Task, TaskStatus, now_iso,
)

FUZZY_CUTOFF = 0.75
_STOP = {"the", "a", "an", "task", "to", "for", "of", "and", "on", "in", "with", "my"}


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", (text or "").lower()) if w not in _STOP}


def _match_score(query: str, title: str) -> float:
    q, t = (query or "").lower().strip(), (title or "").lower().strip()
    if not q:
        return 0.0
    if q == t:
        return 1.0
    ratio = difflib.SequenceMatcher(None, q, t).ratio()
    qw, tw = _words(q), _words(t)
    overlap = len(qw & tw) / len(qw) if qw else 0.0
    # "kitchen screen" should find "Complete kitchen availability screen":
    # every spoken word present counts almost as much as an exact title.
    if qw and qw <= tw:
        overlap = max(overlap, 0.95)
    return max(ratio, overlap)


class TaskManager:
    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn

    def add(self, project_id: int, title: str, description: str | None = None,
            priority=Priority.MEDIUM, status=TaskStatus.TODO) -> tuple[Task, bool]:
        """Returns (task, created). Adding an open task that already exists
        returns the existing one instead of a duplicate."""
        title = " ".join((title or "").split())
        if not title:
            raise ValueError("task title is empty")
        for task in self.list(project_id, OPEN_STATUSES):
            if task.title.lower() == title.lower():
                return task, False
        ts = now_iso()
        status = TaskStatus.parse(status) or TaskStatus.TODO
        cur = self._conn.execute(
            "INSERT INTO tasks (project_id, title, description, status, priority, "
            "created_at, updated_at, completed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (project_id, title, description, status.value, Priority.parse(priority).value,
             ts, ts, ts if status is TaskStatus.DONE else None),
        )
        self._conn.commit()
        return self.get(cur.lastrowid), True

    def get(self, task_id: int) -> Task | None:
        row = self._conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        return Task.from_row(row) if row else None

    def set_status(self, task_id: int, status) -> Task:
        status = TaskStatus.parse(status)
        if status is None:
            raise ValueError("unknown status")
        ts = now_iso()
        self._conn.execute(
            "UPDATE tasks SET status = ?, updated_at = ?, completed_at = ? WHERE id = ?",
            (status.value, ts, ts if status is TaskStatus.DONE else None, task_id),
        )
        self._conn.commit()
        return self.get(task_id)

    def update(self, task_id: int, *, title=None, description=None, priority=None) -> Task:
        fields, values = [], []
        if title:
            fields.append("title = ?")
            values.append(" ".join(title.split()))
        if description is not None:
            fields.append("description = ?")
            values.append(description)
        if priority:
            fields.append("priority = ?")
            values.append(Priority.parse(priority).value)
        if fields:
            fields.append("updated_at = ?")
            values.extend([now_iso(), task_id])
            self._conn.execute(f"UPDATE tasks SET {', '.join(fields)} WHERE id = ?", values)
            self._conn.commit()
        return self.get(task_id)

    def delete(self, task_id: int) -> bool:
        cur = self._conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
        self._conn.commit()
        return cur.rowcount > 0

    def list(self, project_id: int, statuses=None, limit: int | None = None) -> list[Task]:
        rows = self._conn.execute(
            "SELECT * FROM tasks WHERE project_id = ?", (project_id,)
        ).fetchall()
        tasks = [Task.from_row(r) for r in rows]
        if statuses is not None:
            wanted = {TaskStatus.parse(s) for s in statuses}
            tasks = [t for t in tasks if t.status in wanted]
        # In progress, blocked, todo, done; high priority first; then most
        # recently touched first (two stable sorts, newest-first applied first).
        tasks.sort(key=lambda t: t.updated_at, reverse=True)
        tasks.sort(key=lambda t: (STATUS_ORDER[t.status], t.priority.rank if t.is_open else 0))
        return tasks[:limit] if limit else tasks

    def open_tasks(self, project_id: int, limit: int | None = None) -> list[Task]:
        return self.list(project_id, OPEN_STATUSES, limit)

    def completed_since(self, project_id: int, since_iso: str) -> list[Task]:
        rows = self._conn.execute(
            "SELECT * FROM tasks WHERE project_id = ? AND status = 'Done' "
            "AND completed_at >= ? ORDER BY completed_at DESC",
            (project_id, since_iso),
        ).fetchall()
        return [Task.from_row(r) for r in rows]

    def touched_since(self, project_id: int, since_iso: str) -> list[Task]:
        rows = self._conn.execute(
            "SELECT * FROM tasks WHERE project_id = ? AND updated_at >= ? "
            "ORDER BY updated_at DESC",
            (project_id, since_iso),
        ).fetchall()
        return [Task.from_row(r) for r in rows]

    def counts(self, project_id: int) -> dict[str, int]:
        counts = {s.value: 0 for s in TaskStatus}
        for row in self._conn.execute(
            "SELECT status, COUNT(*) AS n FROM tasks WHERE project_id = ? GROUP BY status",
            (project_id,),
        ):
            counts[row["status"]] = row["n"]
        return counts

    def find(self, project_id: int, query: str, include_done: bool = True) -> list[Task]:
        """Tasks matching a spoken title, best tier only (near-ties all
        returned so the caller can ask which one)."""
        if str(query).strip().isdigit():
            task = self.get(int(str(query).strip()))
            return [task] if task and task.project_id == project_id else []
        pool = self.list(project_id) if include_done else self.open_tasks(project_id)
        scored = [(_match_score(query, t.title), t) for t in pool]
        scored = [(s, t) for s, t in scored if s >= FUZZY_CUTOFF]
        if not scored:
            return []
        best = max(s for s, _ in scored)
        tier = [t for s, t in scored if s >= best - 0.05]
        # prefer open tasks when an open and a done one tie
        open_tier = [t for t in tier if t.is_open]
        return open_tier or tier
