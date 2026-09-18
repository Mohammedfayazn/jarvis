"""RecommendationEngine: "what should I work on next?"

Deterministic and explainable: every suggestion comes from one concrete
source (a task, a blocker, the plan you left last session, uncommitted
work, the roadmap in your docs) and carries the reason it was chosen, so
Jarvis can say *why* - and never invents work that is not recorded
somewhere.
"""
from __future__ import annotations

import dataclasses
import datetime as _dt
import re

from .git_analyzer import ChangeSet, Commit
from .models import Entry, Priority, Project, Task, TaskStatus, WorkSession, ago, now


@dataclasses.dataclass(frozen=True)
class Recommendation:
    title: str
    reason: str
    source: str        # task | blocker | plan | git | roadmap | docs | goal | code | setup
    score: int

    def as_dict(self) -> dict:
        return dataclasses.asdict(self)


@dataclasses.dataclass
class ProjectContext:
    """Everything the engine may use. Built by the assistant layer."""
    project: Project
    tasks: list[Task] = dataclasses.field(default_factory=list)
    blockers: list[Entry] = dataclasses.field(default_factory=list)
    next_steps: list[Entry] = dataclasses.field(default_factory=list)
    goals: list[Entry] = dataclasses.field(default_factory=list)
    notes: list[Entry] = dataclasses.field(default_factory=list)
    last_session: WorkSession | None = None
    commits: list[Commit] = dataclasses.field(default_factory=list)
    changes: ChangeSet | None = None
    todo_count: int = 0


_PRIORITY_SCORE = {Priority.HIGH: 75, Priority.MEDIUM: 60, Priority.LOW: 40}


def _key(text: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", text.lower()))


class RecommendationEngine:
    def recommend(self, ctx: ProjectContext, limit: int = 5) -> list[Recommendation]:
        recs: list[Recommendation] = []
        add = recs.append

        for task in ctx.tasks:
            if task.status is TaskStatus.BLOCKED:
                add(Recommendation(f"Unblock: {task.title}",
                                   "It is marked blocked, and blocked work stalls everything after it.",
                                   "blocker", 90))
        for entry in ctx.blockers:
            add(Recommendation(f"Unblock: {entry.text}",
                               f"You recorded this blocker {ago(entry.created_at)}.",
                               "blocker", 88))

        for task in ctx.tasks:
            if task.status is TaskStatus.IN_PROGRESS:
                bonus = 5 if task.priority is Priority.HIGH else 0
                add(Recommendation(f"Finish: {task.title}",
                                   "It is already in progress - finishing beats starting.",
                                   "task", 85 + bonus))

        for entry in ctx.next_steps[:2]:
            add(Recommendation(entry.text,
                               f"You planned this at the end of your last session ({ago(entry.created_at)}).",
                               "plan", 82))

        for task in ctx.tasks:
            if task.status is TaskStatus.TODO:
                add(Recommendation(f"Start: {task.title}",
                                   f"{task.priority.value}-priority open task.",
                                   "task", _PRIORITY_SCORE[task.priority]))

        changes = ctx.changes
        if changes and changes.total:
            last = ctx.commits[0].when if ctx.commits else None
            stale = last is None or now() - last > _dt.timedelta(days=1)
            where = ", ".join(changes.areas(2)) or "the project"
            add(Recommendation(
                f"Commit your uncommitted work ({changes.total} files in {where})",
                ("Nothing is committed since " + ago(last.isoformat()) + "." if last and stale
                 else "Committing now keeps each change easy to review and undo."),
                "git", 70 if (stale or changes.total >= 10) else 50))

        for focus in ctx.project.index.get("current_focus", [])[:1]:
            add(Recommendation(f"Continue the current roadmap phase: {focus}",
                               "Your project docs mark it as current.", "roadmap", 55))
        for item in ctx.project.index.get("doc_open_items", [])[:2]:
            add(Recommendation(item, "Listed as an open checklist item in your docs.", "docs", 45))

        open_tasks = [t for t in ctx.tasks if t.is_open]
        if not open_tasks:
            for goal in ctx.goals[:1]:
                add(Recommendation(f"Break the goal '{goal.text}' into tasks",
                                   "There are no open tasks to move it forward.", "goal", 35))

        if ctx.todo_count:
            add(Recommendation(f"Clear some of the {ctx.todo_count} TODO comments",
                               "Small, low-risk debt reduction.", "code", 25))

        if not ctx.tasks and not ctx.blockers and not ctx.next_steps and not ctx.goals:
            add(Recommendation("Tell me your goals and a few tasks for this project",
                               "I have no tasks or plans recorded for it yet, so my "
                               "suggestions can only come from git and the docs.",
                               "setup", 20))

        # highest score wins; drop repeats of the same idea from different sources
        recs.sort(key=lambda r: r.score, reverse=True)
        seen, unique = set(), []
        for rec in recs:
            key = _key(re.sub(r"^(unblock|finish|start):\s*", "", rec.title, flags=re.I))
            if key in seen:
                continue
            seen.add(key)
            unique.append(rec)
        return unique[:limit]
