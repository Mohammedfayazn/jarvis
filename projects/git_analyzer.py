"""GitAnalyzer: read-only view of a project's git history and working tree.

Only ever runs read commands (log, status, diff, branch, rev-parse) - never
anything that changes the repository. Every call has a timeout so a hung
git (credential prompt, huge repo) cannot freeze Jarvis.
"""
from __future__ import annotations

import dataclasses
import datetime as _dt
import os
import re
import subprocess
from collections import Counter
from pathlib import Path, PurePosixPath

from .models import parse_time

GIT_TIMEOUT = 10
_SEP = "\x1f"
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class GitError(RuntimeError):
    pass


@dataclasses.dataclass(frozen=True)
class Commit:
    sha: str
    author: str
    date: str          # ISO 8601 with the author's offset, straight from git
    subject: str

    @property
    def when(self) -> _dt.datetime | None:
        return parse_time(self.date)

    def as_dict(self) -> dict:
        return dataclasses.asdict(self)


@dataclasses.dataclass(frozen=True)
class ChangeSet:
    branch: str
    upstream: str | None
    ahead: int
    behind: int
    staged: list[str]
    unstaged: list[str]
    untracked: list[str]
    diff_stat: str

    @property
    def files(self) -> list[str]:
        seen = dict.fromkeys(self.staged + self.unstaged + self.untracked)
        return list(seen)

    @property
    def total(self) -> int:
        return len(self.files)

    def areas(self, limit: int = 3) -> list[str]:
        """Folders most of the changes are in: 'lib/features/auth'."""
        counts = Counter()
        for f in self.files:
            parts = PurePosixPath(f.rstrip("/")).parts
            # git lists a new untracked folder as "dir/" - it is all folder
            folder = parts if f.endswith("/") else parts[:-1]
            # three levels is enough to name a feature without naming a file
            key = "/".join(folder[:3]) if folder else parts[0]
            counts[key] += 1
        return [area for area, _ in counts.most_common(limit)]

    def as_dict(self) -> dict:
        data = dataclasses.asdict(self)
        data["total"] = self.total
        data["areas"] = self.areas()
        return data


# Commit-subject vocabulary -> what the work was "about".
THEMES = [
    ("authentication", r"\b(auth\w*|log ?in|sign[- ]?(in|up)|signup|password|oauth|jwt|session)\b"),
    ("profile management", r"\b(profiles?|accounts?|settings|avatar|dashboard)\b"),
    ("UI and screens", r"\b(ui|screens?|pages?|widgets?|theme|design|layout|styl\w*|css|splash|animat\w*|hud|frontend)\b"),
    ("the API", r"\b(api|endpoints?|routes?|rest|graphql|requests?)\b"),
    ("the database", r"\b(db|database|migrations?|schema|sql|tables?|rls|supabase|firestore|sqlite)\b"),
    ("navigation", r"\b(navigation|router|routing|go_router)\b"),
    ("testing", r"\b(tests?|testing|specs?|coverage)\b"),
    ("bug fixes", r"\b(fix\w*|bugs?|crash\w*|errors?|broken)\b"),
    ("refactoring", r"\b(refactor\w*|clean\w*|renam\w*|restructur\w*|simplif\w*|extract\w*)\b"),
    ("documentation", r"\b(docs?|readme|documentation)\b"),
    ("build and tooling", r"\b(build|ci|deps|dependenc\w*|config\w*|scaffold\w*|lint\w*|gitattributes|gitignore|line endings)\b"),
]
_THEME_RE = [(name, re.compile(rx, re.IGNORECASE)) for name, rx in THEMES]


def commit_themes(commits: list[Commit]) -> list[tuple[str, int]]:
    counts = Counter()
    for commit in commits:
        for name, rx in _THEME_RE:
            if rx.search(commit.subject):
                counts[name] += 1
    return counts.most_common()


def _join(items: list[str]) -> str:
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]


def summarize_commits(commits: list[Commit]) -> str:
    """'The last 5 commits focused on authentication and profile management.'"""
    if not commits:
        return "There are no commits yet."
    themes = commit_themes(commits)
    n = len(commits)
    if not themes:
        subjects = "; ".join(c.subject for c in commits[:3])
        return f"The last {n} commits: {subjects}." if n > 1 else f"The last commit: {subjects}."
    top = themes[0][1]
    # keep themes that are genuinely prominent, at most three
    chosen = [name for name, count in themes if count >= max(1, top / 2)][:3]
    if n == 1:
        return f"The last commit was about {_join(chosen)}: {commits[0].subject}."
    return f"The last {n} commits focused on {_join(chosen)}."


class GitAnalyzer:
    def __init__(self, path, timeout: int = GIT_TIMEOUT):
        self.path = Path(path)
        self.timeout = timeout

    def _git(self, *args: str) -> str:
        try:
            proc = subprocess.run(
                ["git", "-C", str(self.path), "-c", "core.quotepath=off",
                 "--no-pager", *args],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=self.timeout, creationflags=_NO_WINDOW,
                env={**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0"},
            )
        except FileNotFoundError as exc:
            raise GitError("git is not installed or not on PATH") from exc
        except subprocess.TimeoutExpired as exc:
            raise GitError(f"git {args[0]} took longer than {self.timeout}s") from exc
        if proc.returncode != 0:
            raise GitError(proc.stderr.strip() or f"git {args[0]} failed")
        return proc.stdout

    def is_repo(self) -> bool:
        try:
            return self._git("rev-parse", "--is-inside-work-tree").strip() == "true"
        except GitError:
            return False

    def has_commits(self) -> bool:
        try:
            self._git("rev-parse", "--verify", "--quiet", "HEAD")
            return True
        except GitError:
            return False

    def get_recent_commits(self, limit: int = 5, since: _dt.datetime | None = None) -> list[Commit]:
        if not self.has_commits():
            return []
        args = ["log", "--no-merges", f"--format=%h{_SEP}%an{_SEP}%aI{_SEP}%s"]
        if limit:
            args.append(f"-n{int(limit)}")
        if since:
            args.append(f"--since={since.isoformat()}")
        commits = []
        for line in self._git(*args).splitlines():
            parts = line.split(_SEP)
            if len(parts) == 4:
                commits.append(Commit(*parts))
        return commits

    def commits_since(self, days: int) -> list[Commit]:
        since = _dt.datetime.now() - _dt.timedelta(days=days)
        return self.get_recent_commits(limit=0, since=since)

    def get_current_branch(self) -> str:
        name = self._git("branch", "--show-current").strip()
        if name:
            return name
        if not self.has_commits():
            # fresh repo: branch exists in name only
            head = self._git("symbolic-ref", "--short", "-q", "HEAD").strip()
            return head or "(no branch)"
        return f"(detached at {self._git('rev-parse', '--short', 'HEAD').strip()})"

    def get_branches(self) -> list[str]:
        out = self._git("branch", "--format=%(refname:short)")
        return [b.strip() for b in out.splitlines() if b.strip()]

    def get_uncommitted_changes(self) -> ChangeSet:
        out = self._git("status", "--porcelain=v1", "-b", "--untracked-files=normal")
        branch, upstream, ahead, behind = "", None, 0, 0
        staged, unstaged, untracked = [], [], []
        for line in out.splitlines():
            if line.startswith("## "):
                # "## main...origin/main [ahead 1, behind 2]" / "## No commits yet on main"
                head = line[3:]
                for prefix in ("No commits yet on ", "Initial commit on "):
                    if head.startswith(prefix):
                        head = head[len(prefix):]
                head, _, info = head.partition(" [")
                branch, _, upstream = head.partition("...")
                upstream = upstream or None
                a = re.search(r"ahead (\d+)", info)
                b = re.search(r"behind (\d+)", info)
                ahead = int(a.group(1)) if a else 0
                behind = int(b.group(1)) if b else 0
                continue
            if len(line) < 4:
                continue
            x, y, name = line[0], line[1], line[3:]
            if " -> " in name:          # rename: report the new name
                name = name.split(" -> ", 1)[1]
            name = name.strip('"')
            if x == "?" and y == "?":
                untracked.append(name)
                continue
            if x not in (" ", "?"):
                staged.append(name)
            if y not in (" ", "?"):
                unstaged.append(name)
        diff_stat = ""
        if self.has_commits():
            stat = self._git("diff", "HEAD", "--shortstat").strip()
            diff_stat = stat
        return ChangeSet(
            branch=(branch if branch and not branch.startswith("HEAD")
                    else self.get_current_branch()),
            upstream=upstream,
            ahead=ahead, behind=behind, staged=staged, unstaged=unstaged,
            untracked=untracked, diff_stat=diff_stat,
        )

    def get_diff(self, path: str | None = None, max_chars: int = 6000) -> str:
        """Unified diff of uncommitted changes (staged + unstaged) vs HEAD."""
        if not self.has_commits():
            return ""
        args = ["diff", "HEAD", "--no-color", "--no-ext-diff"]
        if path:
            args += ["--", path]
        text = self._git(*args)
        if len(text) > max_chars:
            text = text[:max_chars] + f"\n... (diff truncated, {len(text)} chars total)"
        return text

    def get_last_work_session(self) -> dict:
        """When work last happened: the later of the last commit and the
        last edit to a file that is still uncommitted."""
        commits = self.get_recent_commits(1)
        last_commit = commits[0] if commits else None
        last_edit, last_edit_file = None, None
        try:
            changes = self.get_uncommitted_changes()
        except GitError:
            changes = None
        for name in (changes.files if changes else [])[:500]:
            target = self.path / name
            try:
                if target.is_dir():
                    continue
                mtime = _dt.datetime.fromtimestamp(target.stat().st_mtime).replace(microsecond=0)
            except OSError:
                continue          # deleted files have no mtime
            if last_edit is None or mtime > last_edit:
                last_edit, last_edit_file = mtime, name
        commit_time = last_commit.when if last_commit else None
        candidates = [t for t in (commit_time, last_edit) if t]
        return {
            "last_commit": last_commit.as_dict() if last_commit else None,
            "last_edit": last_edit.isoformat() if last_edit else None,
            "last_edit_file": last_edit_file,
            "last_activity": max(candidates).isoformat() if candidates else None,
            "uncommitted_files": changes.total if changes else 0,
        }
