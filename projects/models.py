"""Entities and fixed vocabularies for the project intelligence system.

Pure data: no SQL, no git, no filesystem. Every other layer passes these
around, so they stay small and serialisable (`as_dict` is what the Gemini
tools send back).
"""
from __future__ import annotations

import dataclasses
import datetime as _dt
import json
import re
from enum import Enum


def now() -> _dt.datetime:
    return _dt.datetime.now().replace(microsecond=0)


def now_iso() -> str:
    return now().isoformat()


def parse_time(value) -> _dt.datetime | None:
    """ISO text (ours or git's, with or without an offset) -> naive local time."""
    if not value:
        return None
    if isinstance(value, _dt.datetime):
        moment = value
    else:
        try:
            moment = _dt.datetime.fromisoformat(str(value))
        except ValueError:
            return None
    if moment.tzinfo is not None:
        moment = moment.astimezone().replace(tzinfo=None)
    return moment


def ago(value, reference: _dt.datetime | None = None) -> str:
    """'just now', '3 hours ago', 'yesterday', '2 days ago', '3 weeks ago'."""
    moment = parse_time(value)
    if moment is None:
        return "never"
    seconds = ((reference or now()) - moment).total_seconds()
    if seconds < 90:
        return "just now"
    minutes = seconds / 60
    if minutes < 60:
        return f"{int(minutes)} minutes ago"
    hours = minutes / 60
    if hours < 24:
        return "1 hour ago" if int(hours) == 1 else f"{int(hours)} hours ago"
    days = hours / 24
    if days < 2:
        return "yesterday"
    if days < 14:
        return f"{int(days)} days ago"
    if days < 60:
        return f"{int(days // 7)} weeks ago"
    return f"{int(days // 30)} months ago"


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


class TaskStatus(str, Enum):
    TODO = "Todo"
    IN_PROGRESS = "In Progress"
    BLOCKED = "Blocked"
    DONE = "Done"

    @classmethod
    def parse(cls, text) -> "TaskStatus | None":
        if isinstance(text, cls):
            return text
        key = _norm(str(text or ""))
        return _STATUS_ALIASES.get(key)


_STATUS_ALIASES = {
    "todo": TaskStatus.TODO, "to do": TaskStatus.TODO, "pending": TaskStatus.TODO,
    "open": TaskStatus.TODO, "reopen": TaskStatus.TODO, "not started": TaskStatus.TODO,
    "in progress": TaskStatus.IN_PROGRESS, "inprogress": TaskStatus.IN_PROGRESS,
    "doing": TaskStatus.IN_PROGRESS, "started": TaskStatus.IN_PROGRESS,
    "start": TaskStatus.IN_PROGRESS, "wip": TaskStatus.IN_PROGRESS,
    "working": TaskStatus.IN_PROGRESS,
    "blocked": TaskStatus.BLOCKED, "stuck": TaskStatus.BLOCKED, "block": TaskStatus.BLOCKED,
    "done": TaskStatus.DONE, "complete": TaskStatus.DONE, "completed": TaskStatus.DONE,
    "finished": TaskStatus.DONE, "finish": TaskStatus.DONE, "closed": TaskStatus.DONE,
}

# Order tasks are shown in: what you are doing, what is stuck, what is next.
STATUS_ORDER = {
    TaskStatus.IN_PROGRESS: 0, TaskStatus.BLOCKED: 1, TaskStatus.TODO: 2, TaskStatus.DONE: 3,
}
OPEN_STATUSES = (TaskStatus.IN_PROGRESS, TaskStatus.BLOCKED, TaskStatus.TODO)


class Priority(str, Enum):
    LOW = "Low"
    MEDIUM = "Medium"
    HIGH = "High"

    @classmethod
    def parse(cls, text) -> "Priority":
        if isinstance(text, cls):
            return text
        key = _norm(str(text or ""))
        if key in ("high", "urgent", "important", "critical", "p1", "top", "asap"):
            return cls.HIGH
        if key in ("low", "minor", "later", "p3", "someday"):
            return cls.LOW
        return cls.MEDIUM

    @property
    def rank(self) -> int:
        return {"High": 0, "Medium": 1, "Low": 2}[self.value]


# What a project remembers besides tasks. `preference` and `problem` may be
# global (project_id NULL) - "I prefer small commits" applies everywhere.
ENTRY_KINDS = ("goal", "decision", "note", "blocker", "next_step", "problem", "preference")

_KIND_ALIASES = {
    "goal": "goal", "goals": "goal", "objective": "goal",
    "decision": "decision", "decisions": "decision", "architecture decision": "decision",
    "decided": "decision", "choice": "decision",
    "note": "note", "notes": "note", "implementation note": "note", "idea": "note",
    "blocker": "blocker", "blockers": "blocker", "blocking": "blocker", "blocked": "blocker",
    "next step": "next_step", "next steps": "next_step", "next": "next_step",
    "next_step": "next_step", "plan": "next_step",
    "problem": "problem", "problems": "problem", "recurring problem": "problem",
    "issue": "problem", "bug": "problem",
    "preference": "preference", "preferences": "preference",
    "coding preference": "preference", "style": "preference",
}


def normalize_kind(text) -> str | None:
    key = _norm(str(text or ""))
    if key.replace(" ", "_") in ENTRY_KINDS:
        return key.replace(" ", "_")
    return _KIND_ALIASES.get(key)


@dataclasses.dataclass(frozen=True)
class Project:
    id: int
    name: str
    path: str
    description: str | None
    current_status: str | None
    index: dict
    last_indexed_at: str | None
    last_opened_at: str | None
    last_worked_on: str | None
    created_at: str
    updated_at: str

    @classmethod
    def from_row(cls, row) -> "Project":
        try:
            index = json.loads(row["index_json"]) if row["index_json"] else {}
        except (TypeError, ValueError):
            index = {}
        return cls(
            id=row["id"], name=row["name"], path=row["path"],
            description=row["description"], current_status=row["current_status"],
            index=index, last_indexed_at=row["last_indexed_at"],
            last_opened_at=row["last_opened_at"], last_worked_on=row["last_worked_on"],
            created_at=row["created_at"], updated_at=row["updated_at"],
        )

    def as_dict(self) -> dict:
        data = dataclasses.asdict(self)
        data.pop("index")  # large; tools send the summary text instead
        return data


@dataclasses.dataclass(frozen=True)
class Task:
    id: int
    project_id: int
    title: str
    description: str | None
    status: TaskStatus
    priority: Priority
    created_at: str
    updated_at: str
    completed_at: str | None

    @classmethod
    def from_row(cls, row) -> "Task":
        return cls(
            id=row["id"], project_id=row["project_id"], title=row["title"],
            description=row["description"], status=TaskStatus(row["status"]),
            priority=Priority(row["priority"]), created_at=row["created_at"],
            updated_at=row["updated_at"], completed_at=row["completed_at"],
        )

    @property
    def is_open(self) -> bool:
        return self.status is not TaskStatus.DONE

    def as_dict(self) -> dict:
        data = dataclasses.asdict(self)
        data["status"] = self.status.value
        data["priority"] = self.priority.value
        return data


@dataclasses.dataclass(frozen=True)
class Entry:
    id: int
    project_id: int | None
    kind: str
    text: str
    resolved: bool
    created_at: str
    resolved_at: str | None

    @classmethod
    def from_row(cls, row) -> "Entry":
        return cls(
            id=row["id"], project_id=row["project_id"], kind=row["kind"],
            text=row["text"], resolved=bool(row["resolved"]),
            created_at=row["created_at"], resolved_at=row["resolved_at"],
        )

    def as_dict(self) -> dict:
        return dataclasses.asdict(self)


@dataclasses.dataclass(frozen=True)
class WorkSession:
    id: int
    project_id: int
    started_at: str
    ended_at: str | None
    accomplished: str | None
    next_steps: str | None

    @classmethod
    def from_row(cls, row) -> "WorkSession":
        return cls(
            id=row["id"], project_id=row["project_id"], started_at=row["started_at"],
            ended_at=row["ended_at"], accomplished=row["accomplished"],
            next_steps=row["next_steps"],
        )

    def as_dict(self) -> dict:
        return dataclasses.asdict(self)


@dataclasses.dataclass(frozen=True)
class ProjectMemory:
    """Everything ProjectBrain knows about one project, in the shape of the
    spec's field list."""
    project_name: str
    project_path: str
    project_description: str | None
    goals: list[str]
    current_status: str | None
    completed_tasks: list[Task]
    pending_tasks: list[Task]
    blockers: list[str]
    decisions: list[str]
    notes: list[str]
    last_worked_on: str | None
    next_steps: list[str]

    def as_dict(self) -> dict:
        data = dataclasses.asdict(self)
        data["completed_tasks"] = [t.as_dict() for t in self.completed_tasks]
        data["pending_tasks"] = [t.as_dict() for t in self.pending_tasks]
        return data
