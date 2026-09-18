"""Application layer: the project co-pilot Jarvis talks to.

Composes ProjectBrain (memory), TaskManager, GitAnalyzer, ProjectIndexer,
CodeInsights, ProjectLocator, VSCodeController and RecommendationEngine
into use cases ("open Homemade and summarise it", "what changed this
week?"). Every public method returns a dict with a plain-language
"message" - the contract all of Jarvis's tools follow - plus the
structured data behind it.

Every method takes an optional project name; without one it uses the
project opened most recently, so follow-up questions just work.
"""
from __future__ import annotations

import datetime as _dt
import logging
import re
from pathlib import Path

from . import db
from .brain import ProjectBrain
from .code_insights import CodeInsights
from .git_analyzer import GitAnalyzer, GitError, summarize_commits
from .indexer import ProjectIndexer, summary_text
from .locator import ProjectLocator
from .models import (
    ENTRY_KINDS, Priority, Project, TaskStatus, ago, normalize_kind, now, parse_time,
)
from .recommend import ProjectContext, RecommendationEngine
from .vscode import VSCodeController

logger = logging.getLogger("jarvis.projects")

REINDEX_AFTER = _dt.timedelta(minutes=10)
_LAST_WORDS = {"last", "current", "this", "same", "previous", "recent", "it", "that"}
_KIND_LABEL = {
    "goal": "Goals", "decision": "Decisions", "note": "Notes", "blocker": "Blockers",
    "next_step": "Planned next steps", "problem": "Recurring problems",
    "preference": "Coding preferences",
}


def _bullets(items, limit: int = 5) -> str:
    items = [i for i in items if i]
    shown = "\n".join(f"* {i}" for i in items[:limit])
    if len(items) > limit:
        shown += f"\n* ...and {len(items) - limit} more"
    return shown


def _task_line(task) -> str:
    tag = "" if task.status is TaskStatus.TODO else f" [{task.status.value}]"
    prio = " (high priority)" if task.priority is Priority.HIGH and task.is_open else ""
    return f"{task.title}{tag}{prio}"


class ProjectAssistant:
    def __init__(self, db_path=None, roots=None, vscode: VSCodeController | None = None,
                 locator: ProjectLocator | None = None):
        path = Path(db_path) if db_path else db.DEFAULT_DB_PATH
        self._conn = db.connect(path)
        self.brain = ProjectBrain(self._conn)
        self.locator = locator or ProjectLocator(roots)
        self.vscode = vscode or VSCodeController()
        self.engine = RecommendationEngine()
        self.notes_dir = path.parent / "notes"

    def close(self) -> None:
        self._conn.close()

    # -- resolving "which project?" ----------------------------------------

    def _resolve(self, name: str | None):
        """-> (project, None) or (None, error_result)."""
        words = [w for w in re.findall(r"[a-z0-9]+", (name or "").lower())
                 if w not in {"project", "the", "my", "open", "repo"}]
        if not words or all(w in _LAST_WORDS for w in words):
            project = self.brain.last_opened()
            if project:
                return project, None
            return None, {"ok": False, "message":
                          "Which project? I don't have a current project yet - "
                          "say 'open <name> project' first."}

        matches = self.brain.find(name)
        if len(matches) == 1:
            return matches[0], None
        if len(matches) > 1:
            return None, self._choice([(p.name, p.path) for p in matches])

        folders = self.locator.locate(name)
        if not folders:
            roots = ", ".join(str(r) for r in self.locator.roots) or "(no search folders exist)"
            return None, {"ok": False, "not_found": True, "message":
                          f"I couldn't find a project called '{name}'. I looked in {roots}. "
                          "Set JARVIS_PROJECT_ROOTS if your projects live elsewhere."}
        if len(folders) > 1:
            return None, self._choice([(f.name, str(f)) for f in folders])
        folder = folders[0]
        return self.brain.get_by_path(folder) or self.brain.register(folder.name, folder), None

    @staticmethod
    def _choice(options) -> dict:
        listed = "; ".join(f"{n} ({p})" for n, p in options[:5])
        return {"ok": False, "needs_choice": True,
                "candidates": [{"name": n, "path": p} for n, p in options],
                "message": f"More than one project matches: {listed}. Which one?"}

    def _with(self, name, fn):
        project, error = self._resolve(name)
        if error:
            return error
        if not Path(project.path).is_dir():
            return {"ok": False, "message":
                    f"{project.name}'s folder ({project.path}) no longer exists."}
        try:
            return fn(project)
        except Exception as exc:  # a tool must answer, never crash the session
            logger.exception("project tool failed")
            return {"ok": False, "message": f"Something went wrong: {exc}"}

    # -- building blocks ---------------------------------------------------

    def _index(self, project: Project, force: bool = False) -> Project:
        fresh = parse_time(project.last_indexed_at)
        if force or not fresh or now() - fresh > REINDEX_AFTER:
            index = ProjectIndexer(project.path).index()
            project = self.brain.save_index(project.id, index, index.get("description"))
        return project

    @staticmethod
    def _git(project: Project) -> GitAnalyzer | None:
        git = GitAnalyzer(project.path)
        return git if git.is_repo() else None

    def _context(self, project: Project, commits: int = 5, todos: bool = False) -> ProjectContext:
        ctx = ProjectContext(
            project=project,
            tasks=self.brain.tasks.list(project.id),
            blockers=self.brain.entries(project.id, ["blocker"], include_global=False),
            next_steps=self.brain.entries(project.id, ["next_step"], include_global=False, limit=2),
            goals=self.brain.entries(project.id, ["goal"], include_global=False, limit=5),
            notes=self.brain.entries(project.id, ["note", "decision"], include_global=False, limit=5),
            last_session=self.brain.last_session(project.id),
        )
        git = self._git(project)
        if git:
            try:
                ctx.commits = git.get_recent_commits(commits)
                ctx.changes = git.get_uncommitted_changes()
            except GitError as exc:
                logger.warning("git failed for %s: %s", project.name, exc)
        if todos:
            ctx.todo_count = CodeInsights(project.path).find_todos(limit=1)["count"]
        return ctx

    @staticmethod
    def _last_activity(project: Project, ctx: ProjectContext):
        """Latest of: a summarised session, the last commit, the newest edit
        to a still-uncommitted file."""
        times = []
        if project.last_worked_on:
            times.append(parse_time(project.last_worked_on))
        if ctx.commits and ctx.commits[0].when:
            times.append(ctx.commits[0].when)
        if ctx.changes:
            root = Path(project.path)
            for name in ctx.changes.files[:300]:
                try:
                    path = root / name
                    if path.is_file():
                        times.append(_dt.datetime.fromtimestamp(path.stat().st_mtime).replace(microsecond=0))
                except OSError:
                    continue
        times = [t for t in times if t]
        return max(times) if times else None

    def _git_lines(self, ctx: ProjectContext) -> list[str]:
        lines = []
        changes = ctx.changes
        if changes:
            if changes.total:
                areas = ", ".join(changes.areas(2))
                lines.append(f"Branch {changes.branch}: {changes.total} uncommitted "
                             f"file(s)" + (f", mostly in {areas}." if areas else "."))
            else:
                lines.append(f"Branch {changes.branch}: everything is committed.")
            if changes.ahead:
                lines.append(f"{changes.ahead} commit(s) not pushed yet.")
        if ctx.commits:
            lines.append("Recent Git activity: " + summarize_commits(ctx.commits))
            lines.append(_bullets([c.subject for c in ctx.commits], 3))
        elif changes is not None:
            lines.append("No commits yet.")
        return lines

    def _report(self, project: Project, ctx: ProjectContext, header: str, resume: bool) -> str:
        lines = [header]
        last = self._last_activity(project, ctx)
        lines.append(f"Last active: {ago(last.isoformat()) if last else 'no activity recorded'}.")
        if project.description and not resume:
            lines.append(project.description.rstrip(".") + ".")
        lines += self._git_lines(ctx)

        if resume:
            lines += self._resume_lines(project, ctx)

        open_tasks = [t for t in ctx.tasks if t.is_open]
        if open_tasks:
            lines.append("Open tasks:\n" + _bullets([_task_line(t) for t in open_tasks], 4))
        else:
            lines.append("No open tasks recorded.")
        blockers = [e.text for e in ctx.blockers]
        if blockers:
            lines.append("Blockers:\n" + _bullets(blockers, 3))

        recs = self.engine.recommend(ctx, limit=1)
        if recs:
            lines.append(f"Suggested next step: {recs[0].title}."
                         + (f" ({recs[0].reason})" if resume else ""))
        return "\n".join(lines)

    def _resume_lines(self, project: Project, ctx: ProjectContext) -> list[str]:
        lines = []
        session = ctx.last_session
        if session:
            when = ago(session.ended_at)
            if session.accomplished:
                lines.append(f"Last session ({when}) you finished: {session.accomplished}.")
            if session.next_steps:
                lines.append(f"You planned to do next: {session.next_steps}.")
        in_progress = [t.title for t in ctx.tasks if t.status is TaskStatus.IN_PROGRESS]
        if in_progress:
            lines.append("In progress:\n" + _bullets(in_progress, 3))
        if ctx.changes and ctx.changes.total and not session:
            lines.append("You were in the middle of changes in "
                         + ", ".join(ctx.changes.areas(3)) + ".")
        return lines

    # -- desktop integration -------------------------------------------------

    def open_project(self, name: str | None = None, mode: str = "summary",
                     open_editor: bool = True) -> dict:
        """mode: 'open' (just open), 'summary' (status), 'resume' (+ where you left off)."""
        mode = (mode or "summary").lower()
        mode = "resume" if mode.startswith("resum") else "open" if mode == "open" else "summary"

        def run(project: Project) -> dict:
            project = self._index(project)
            editor_note = ""
            if open_editor:
                ok, msg = self.vscode.open(project.path)
                editor_note = " in VS Code" if ok else f" (VS Code: {msg})"
            self.brain.mark_opened(project.id)
            self.brain.start_session(project.id)
            header = f"{project.name} project opened{editor_note}."
            ctx = self._context(project)
            if mode == "open":
                last = self._last_activity(project, ctx)
                message = header + (f" Last active {ago(last.isoformat())}." if last else "")
            else:
                message = self._report(project, ctx, header, resume=(mode == "resume"))
            return {"ok": True, "project": project.as_dict(), "mode": mode,
                    "recommendations": [r.as_dict() for r in self.engine.recommend(ctx)],
                    "message": message}

        return self._with(name, run)

    def list_projects(self) -> dict:
        projects = self.brain.list_projects()
        if not projects:
            return {"ok": True, "projects": [],
                    "message": "No projects yet. Say 'open <name> project' and I'll find it."}
        lines = [f"{p.name} (opened {ago(p.last_opened_at)})" for p in projects]
        return {"ok": True, "projects": [p.as_dict() for p in projects],
                "message": "Projects I know:\n" + _bullets(lines, 10)}

    def open_notes(self, project: str | None = None) -> dict:
        def run(p: Project) -> dict:
            path = self._write_notes(p)
            ok, msg = self.vscode.open(path)
            return {"ok": ok, "path": str(path),
                    "message": f"{p.name} notes written to {path.name}"
                               + (" and opened in VS Code." if ok else f". {msg}")}
        return self._with(project, run)

    def _write_notes(self, project: Project) -> Path:
        memory = self.brain.memory(project)
        out = [f"# {project.name} - project notes", "",
               f"Path: `{project.path}`", f"Last worked on: {ago(project.last_worked_on)}", ""]
        if memory.project_description:
            out += [memory.project_description, ""]
        if memory.current_status:
            out += ["## Current status", memory.current_status, ""]
        sections = [
            ("Goals", memory.goals), ("Planned next steps", memory.next_steps),
            ("Blockers", memory.blockers),
            ("Open tasks", [_task_line(t) for t in memory.pending_tasks]),
            ("Recently completed", [t.title for t in memory.completed_tasks]),
            ("Decisions", memory.decisions), ("Notes", memory.notes),
        ]
        for title, items in sections:
            if items:
                out += [f"## {title}", *[f"- {i}" for i in items], ""]
        sessions = self.brain.sessions_since(project.id, "0000")[:10]
        if sessions:
            out.append("## Work sessions")
            for s in sessions:
                out.append(f"- {s.ended_at[:10]}: {s.accomplished or '-'}"
                           + (f" -> next: {s.next_steps}" if s.next_steps else ""))
            out.append("")
        self.notes_dir.mkdir(parents=True, exist_ok=True)
        safe = re.sub(r"[^A-Za-z0-9_-]+", "-", project.name).strip("-") or "project"
        path = self.notes_dir / f"{safe}.md"
        path.write_text("\n".join(out), encoding="utf-8")
        return path

    # -- project chat --------------------------------------------------------

    def project_status(self, project: str | None = None) -> dict:
        def run(p: Project) -> dict:
            p = self._index(p)
            ctx = self._context(p)
            message = self._report(p, ctx, f"{p.name} status.", resume=False)
            if p.current_status:
                message += f"\nLast recorded status: {p.current_status}."
            return {"ok": True, "memory": self.brain.memory(p).as_dict(), "message": message}
        return self._with(project, run)

    def what_was_i_working_on(self, project: str | None = None) -> dict:
        def run(p: Project) -> dict:
            ctx = self._context(p, commits=3)
            lines = self._resume_lines(p, ctx)
            if ctx.commits:
                lines.append(f"Last commit ({ago(ctx.commits[0].date)}): {ctx.commits[0].subject}.")
            if ctx.changes and ctx.changes.total and ctx.last_session:
                lines.append(f"Uncommitted: {ctx.changes.total} file(s) in "
                             + ", ".join(ctx.changes.areas(3)) + ".")
            if not lines:
                lines.append("I have no record of recent work on it - no sessions, "
                             "tasks in progress, or commits.")
            return {"ok": True, "message": f"{p.name}: " + "\n".join(lines)}
        return self._with(project, run)

    def what_changed(self, days: int = 7, project: str | None = None) -> dict:
        days = max(1, min(int(days or 7), 365))

        def run(p: Project) -> dict:
            since = now() - _dt.timedelta(days=days)
            since_iso = since.isoformat()
            period = "today" if days == 1 else "this week" if days == 7 else f"in the last {days} days"
            lines = [f"{p.name} - what changed {period}:"]
            git = self._git(p)
            commits = []
            if git:
                try:
                    commits = git.commits_since(days)
                except GitError as exc:
                    lines.append(f"(git unavailable: {exc})")
            if commits:
                lines.append(f"{len(commits)} commit(s). " + summarize_commits(commits[:10]))
                lines.append(_bullets([c.subject for c in commits], 6))
            else:
                lines.append("No commits.")
            done = self.brain.tasks.completed_since(p.id, since_iso)
            if done:
                lines.append("Tasks completed:\n" + _bullets([t.title for t in done], 5))
            sessions = self.brain.sessions_since(p.id, since_iso)
            if sessions:
                lines.append("From your work sessions:\n"
                             + _bullets([s.accomplished for s in sessions if s.accomplished], 5))
            decisions = self.brain.entries(p.id, ["decision"], include_global=False,
                                           include_resolved=True, since=since_iso)
            if decisions:
                lines.append("Decisions:\n" + _bullets([d.text for d in decisions], 3))
            blockers = self.brain.entries(p.id, ["blocker"], include_global=False, since=since_iso)
            if blockers:
                lines.append("New blockers:\n" + _bullets([b.text for b in blockers], 3))
            return {"ok": True, "commits": [c.as_dict() for c in commits],
                    "completed_tasks": [t.as_dict() for t in done],
                    "message": "\n".join(lines)}
        return self._with(project, run)

    def next_actions(self, project: str | None = None) -> dict:
        def run(p: Project) -> dict:
            p = self._index(p)
            ctx = self._context(p, todos=True)
            recs = self.engine.recommend(ctx, limit=5)
            lines = [f"{p.name} - what to work on next:"]
            lines += [f"{i}. {r.title} - {r.reason}" for i, r in enumerate(recs, 1)]
            goals = [g.text for g in ctx.goals]
            if goals:
                lines.append("Your goals: " + "; ".join(goals[:3]) + ".")
            return {"ok": True, "recommendations": [r.as_dict() for r in recs],
                    "message": "\n".join(lines)}
        return self._with(project, run)

    def daily_summary(self, hours: int = 24) -> dict:
        since = now() - _dt.timedelta(hours=hours)
        since_iso = since.isoformat()
        worked, completed, in_progress, blocked, suggestions = [], [], [], [], []
        for p in self.brain.list_projects():
            if not Path(p.path).is_dir():
                continue
            commits = []
            git = self._git(p)
            if git:
                try:
                    commits = git.get_recent_commits(limit=0, since=since)
                except GitError:
                    commits = []
            done = self.brain.tasks.completed_since(p.id, since_iso)
            sessions = self.brain.sessions_since(p.id, since_iso)
            touched = self.brain.tasks.touched_since(p.id, since_iso)
            opened = (parse_time(p.last_opened_at) or _dt.datetime.min) >= since
            if not (commits or done or sessions or touched or opened):
                continue
            worked.append(f"{p.name}" + (f" ({len(commits)} commit(s))" if commits else ""))
            completed += [f"{p.name}: {t.title}" for t in done]
            completed += [f"{p.name}: {s.accomplished}" for s in sessions if s.accomplished]
            if not done and not sessions and commits:
                completed.append(f"{p.name}: " + summarize_commits(commits))
            tasks = self.brain.tasks.list(p.id)
            in_progress += [f"{p.name}: {t.title}" for t in tasks if t.status is TaskStatus.IN_PROGRESS]
            blocked += [f"{p.name}: {t.title}" for t in tasks if t.status is TaskStatus.BLOCKED]
            blocked += [f"{p.name}: {e.text}" for e in
                        self.brain.entries(p.id, ["blocker"], include_global=False)]
            recs = self.engine.recommend(self._context(p, commits=3), limit=1)
            if recs:
                suggestions.append(f"{p.name}: {recs[0].title}")
        if not worked:
            return {"ok": True, "message":
                    f"No project activity in the last {hours} hours - no commits, "
                    "sessions or task changes that I know of."}
        none = lambda xs: _bullets(xs, 6) if xs else "* nothing"
        message = "\n".join([
            "Projects worked on:\n" + _bullets(worked, 6),
            "Completed:\n" + none(completed),
            "In progress:\n" + none(in_progress),
            "Blocked:\n" + none(blocked),
            "Suggested next actions:\n" + none(suggestions),
        ])
        return {"ok": True, "message": message}

    # -- work sessions -----------------------------------------------------

    def end_work_session(self, accomplished: str | None = None, next_steps: str | None = None,
                         project: str | None = None) -> dict:
        if not (accomplished or "").strip() and not (next_steps or "").strip():
            return {"ok": False, "message":
                    "Ask what was accomplished today and what should be done next first."}

        def run(p: Project) -> dict:
            session = self.brain.end_session(p.id, accomplished, next_steps)
            parts = [f"Saved today's session for {p.name}."]
            if session.accomplished:
                parts.append(f"Done: {session.accomplished}.")
            if session.next_steps:
                parts.append(f"Next time: {session.next_steps}.")
            return {"ok": True, "session": session.as_dict(), "message": " ".join(parts)}
        return self._with(project, run)

    # -- tasks ---------------------------------------------------------------

    def add_task(self, title: str, priority: str | None = None, description: str | None = None,
                 status: str | None = None, project: str | None = None) -> dict:
        if not (title or "").strip():
            return {"ok": False, "message": "What is the task?"}

        def run(p: Project) -> dict:
            task, created = self.brain.tasks.add(
                p.id, title, description, Priority.parse(priority),
                TaskStatus.parse(status) or TaskStatus.TODO)
            msg = (f"Added to {p.name}: {task.title} ({task.priority.value} priority)."
                   if created else f"'{task.title}' is already an open task ({task.status.value}).")
            return {"ok": True, "created": created, "task": task.as_dict(), "message": msg}
        return self._with(project, run)

    def update_task(self, title: str, status: str | None = None, priority: str | None = None,
                    project: str | None = None) -> dict:
        new_status = TaskStatus.parse(status) if status else None
        if status and new_status is None:
            return {"ok": False, "message":
                    f"'{status}' is not a status. Use Todo, In Progress, Blocked or Done."}

        def run(p: Project) -> dict:
            matches = self.brain.tasks.find(p.id, title)
            if not matches:
                open_titles = [t.title for t in self.brain.tasks.open_tasks(p.id, 6)]
                hint = (" Open tasks: " + "; ".join(open_titles) + ".") if open_titles else ""
                return {"ok": False, "message": f"No task in {p.name} matches '{title}'.{hint}"}
            if len(matches) > 1:
                return {"ok": False, "needs_choice": True,
                        "candidates": [t.as_dict() for t in matches],
                        "message": "Which task? " + "; ".join(t.title for t in matches[:5])}
            task = matches[0]
            if priority:
                task = self.brain.tasks.update(task.id, priority=priority)
            if new_status:
                task = self.brain.tasks.set_status(task.id, new_status)
            verb = {TaskStatus.DONE: "marked complete", TaskStatus.BLOCKED: "marked blocked",
                    TaskStatus.IN_PROGRESS: "marked in progress",
                    TaskStatus.TODO: "moved back to todo"}.get(new_status, "updated")
            return {"ok": True, "task": task.as_dict(), "message": f"'{task.title}' {verb}."}
        return self._with(project, run)

    def list_tasks(self, status: str | None = "pending", project: str | None = None) -> dict:
        key = (status or "pending").strip().lower()

        def run(p: Project) -> dict:
            if key in ("pending", "open", "next", "remaining", ""):
                tasks, label = self.brain.tasks.open_tasks(p.id), "Pending tasks"
            elif key in ("all", "everything"):
                tasks, label = self.brain.tasks.list(p.id), "All tasks"
            else:
                wanted = TaskStatus.parse(key)
                if wanted is None:
                    return {"ok": False, "message": f"Unknown status '{status}'."}
                tasks = self.brain.tasks.list(p.id, [wanted], limit=20)
                label = {TaskStatus.DONE: "Completed tasks", TaskStatus.BLOCKED: "Blocked tasks",
                         TaskStatus.IN_PROGRESS: "Tasks in progress",
                         TaskStatus.TODO: "Todo tasks"}[wanted]
            if not tasks:
                return {"ok": True, "tasks": [], "message": f"{p.name}: no {label.lower()}."}
            lines = [f"{t.title} (done {ago(t.completed_at)})" if t.status is TaskStatus.DONE
                     else _task_line(t) for t in tasks]
            return {"ok": True, "tasks": [t.as_dict() for t in tasks],
                    "message": f"{p.name} - {label}:\n" + _bullets(lines, 8)}
        return self._with(project, run)

    # -- long-term memory ----------------------------------------------------

    def record(self, kind: str, text: str, project: str | None = None,
               scope: str = "project") -> dict:
        kind_key = normalize_kind(kind)
        if kind_key not in ENTRY_KINDS:
            return {"ok": False, "message":
                    f"'{kind}' is not something I track. Use: {', '.join(ENTRY_KINDS)}."}
        if not (text or "").strip():
            return {"ok": False, "message": "What should I record?"}
        if (scope or "").lower() == "global" and kind_key in ("preference", "problem"):
            entry = self.brain.add_entry(None, kind_key, text)
            return {"ok": True, "entry": entry.as_dict(),
                    "message": f"Noted for all projects ({kind_key}): {entry.text}."}

        def run(p: Project) -> dict:
            entry = self.brain.add_entry(p.id, kind_key, text)
            return {"ok": True, "entry": entry.as_dict(),
                    "message": f"Recorded {kind_key.replace('_', ' ')} for {p.name}: {entry.text}."}
        return self._with(project, run)

    def recall(self, kind: str | None = None, query: str | None = None,
               project: str | None = None) -> dict:
        kinds = [normalize_kind(kind)] if kind and normalize_kind(kind) else None

        def run(p: Project) -> dict:
            if query:
                entries = self.brain.find_entries(p.id, query, kinds)
                entries += [e for e in self.brain.entries(p.id, kinds, include_resolved=True)
                            if e.resolved and query.lower() in e.text.lower()]
            else:
                entries = self.brain.entries(p.id, kinds, limit=30)
            if not entries:
                what = f" about '{query}'" if query else ""
                return {"ok": True, "found": False, "entries": [],
                        "message": f"Nothing recorded for {p.name}{what}."}
            grouped: dict[str, list[str]] = {}
            for e in entries:
                scope = "" if e.project_id else " (all projects)"
                stamp = f" - {ago(e.created_at)}{scope}"
                grouped.setdefault(e.kind, []).append(e.text + stamp)
            lines = [f"{_KIND_LABEL[k]}:\n" + _bullets(v, 5) for k, v in grouped.items()]
            return {"ok": True, "found": True, "entries": [e.as_dict() for e in entries],
                    "message": f"{p.name}:\n" + "\n".join(lines)}
        return self._with(project, run)

    def blockers(self, project: str | None = None) -> dict:
        def run(p: Project) -> dict:
            tasks = self.brain.tasks.list(p.id, [TaskStatus.BLOCKED])
            entries = self.brain.entries(p.id, ["blocker"], include_global=False)
            items = [f"{t.title} (task, blocked {ago(t.updated_at)})" for t in tasks]
            items += [f"{e.text} (noted {ago(e.created_at)})" for e in entries]
            if not items:
                return {"ok": True, "blockers": [],
                        "message": f"Nothing is recorded as blocking {p.name}."}
            return {"ok": True, "blockers": items,
                    "message": f"{p.name} - blocking progress:\n" + _bullets(items, 6)}
        return self._with(project, run)

    def resolve_blocker(self, text: str, project: str | None = None) -> dict:
        def run(p: Project) -> dict:
            matches = self.brain.find_entries(p.id, text, ["blocker"])
            if not matches:
                blocked = self.brain.tasks.find(p.id, text, include_done=False)
                blocked = [t for t in blocked if t.status is TaskStatus.BLOCKED]
                if len(blocked) == 1:
                    task = self.brain.tasks.set_status(blocked[0].id, TaskStatus.IN_PROGRESS)
                    return {"ok": True, "message": f"'{task.title}' is unblocked and back in progress."}
                return {"ok": False, "message": f"No open blocker in {p.name} matches '{text}'."}
            if len(matches) > 1:
                return {"ok": False, "needs_choice": True,
                        "message": "Which blocker? " + "; ".join(e.text for e in matches[:5])}
            entry = self.brain.resolve_entry(matches[0].id)
            return {"ok": True, "message": f"Blocker resolved: {entry.text}."}
        return self._with(project, run)

    # -- engineering assistant -----------------------------------------------

    def code(self, action: str, query: str | None = None, project: str | None = None,
             start: int | None = None) -> dict:
        action = (action or "").strip().lower()

        def run(p: Project) -> dict:
            insights = CodeInsights(p.path)
            if action in ("architecture", "explain_architecture", "structure", "overview"):
                p2 = self._index(p)
                index = p2.index
                layout = ", ".join(f"{i['folder']} ({i['files']})" for i in index.get("layout", [])[:10])
                notes = index.get("architecture_notes") or "No architecture section in the docs."
                return {"ok": True, "summary": summary_text(index), "layout": index.get("layout"),
                        "architecture_notes": notes,
                        "message": f"{summary_text(index)}\nFolders (source files): {layout}.\n"
                                   f"From the docs:\n{notes}"}
            if action in ("search", "find"):
                return insights.search(query or "")
            if action in ("todos", "todo", "find_todos"):
                return insights.find_todos()
            if action in ("read", "explain", "show", "open_file"):
                if not query:
                    return {"ok": False, "message": "Which file?"}
                return insights.read(query, start or 1)
            if action in ("module", "summarize", "summary", "summarize_module"):
                if not query:
                    return {"ok": False, "message": "Which module or folder?"}
                return insights.module_summary(query)
            if action in ("health", "refactor", "tests", "missing_tests", "debt",
                          "technical_debt", "review"):
                return insights.health()
            if action in ("diff", "changes"):
                git = self._git(p)
                if not git:
                    return {"ok": False, "message": f"{p.name} is not a git repository."}
                changes = git.get_uncommitted_changes()
                diff = git.get_diff(query) if query else git.get_diff()
                return {"ok": True, "changes": changes.as_dict(), "diff": diff,
                        "message": (f"{changes.total} uncommitted file(s)"
                                    + (f" ({changes.diff_stat})" if changes.diff_stat else "")
                                    + (": " + ", ".join(changes.files[:8]) if changes.files else "")
                                    + ".")}
            return {"ok": False, "message":
                    "Unknown code action. Use architecture, search, todos, read, module, health or diff."}
        return self._with(project, run)


_assistant: ProjectAssistant | None = None


def configure(db_path=None, roots=None, vscode=None) -> ProjectAssistant:
    """Point the shared assistant at a specific database (tests, demos)."""
    global _assistant
    if _assistant is not None:
        _assistant.close()
    _assistant = ProjectAssistant(db_path, roots, vscode)
    return _assistant


def get_assistant() -> ProjectAssistant:
    global _assistant
    if _assistant is None:
        _assistant = ProjectAssistant()
    return _assistant
