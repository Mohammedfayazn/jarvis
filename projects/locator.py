"""Find a project folder on disk from a spoken name ("Homemade" ->
C:\\Users\\me\\dev\\homemade).

Searches a few usual roots two levels deep, and only counts folders that
look like projects (a .git folder or a manifest), so "open documents
project" never opens your whole Documents folder.
"""
from __future__ import annotations

import difflib
import os
from pathlib import Path

from .brain import normalize_name
from .code_insights import SKIP_DIRS

ROOTS_ENV = "JARVIS_PROJECT_ROOTS"
DEFAULT_ROOT_NAMES = ("dev", "Documents", "source/repos", "projects", "Projects",
                      "code", "repos", "src", "Desktop", "workspace")
PROJECT_MARKERS = (".git", "pubspec.yaml", "package.json", "pyproject.toml",
                   "requirements.txt", "go.mod", "Cargo.toml", "pom.xml",
                   "build.gradle", "main.py")
MATCH_CUTOFF = 0.8
MAX_DIRS = 3000


def default_roots() -> list[Path]:
    configured = os.environ.get(ROOTS_ENV)
    if configured:
        roots = [Path(p).expanduser() for p in configured.split(os.pathsep) if p.strip()]
    else:
        home = Path.home()
        roots = [home / name for name in DEFAULT_ROOT_NAMES]
    seen, out = set(), []
    for root in roots:
        try:
            resolved = root.resolve()
        except OSError:
            continue
        if resolved.is_dir() and resolved not in seen:
            seen.add(resolved)
            out.append(resolved)
    return out


def looks_like_project(folder: Path) -> bool:
    try:
        if any((folder / marker).exists() for marker in PROJECT_MARKERS):
            return True
        return any(folder.glob("*.sln")) or any(folder.glob("*.csproj"))
    except OSError:
        return False


class ProjectLocator:
    def __init__(self, roots: list[Path] | None = None):
        self.roots = [Path(r).resolve() for r in roots] if roots is not None else default_roots()

    def candidates(self):
        """Project folders under the roots: root/x and root/x/y."""
        seen = 0
        for root in self.roots:
            for child in self._subdirs(root):
                seen += 1
                if seen > MAX_DIRS:
                    return
                if looks_like_project(child):
                    yield child
                    continue       # don't descend into a project's own folders
                for grandchild in self._subdirs(child):
                    seen += 1
                    if looks_like_project(grandchild):
                        yield grandchild

    @staticmethod
    def _subdirs(folder: Path) -> list[Path]:
        try:
            return sorted(
                p for p in folder.iterdir()
                if p.is_dir() and not p.name.startswith((".", "$"))
                and p.name not in SKIP_DIRS and p.name != "AppData"
            )
        except OSError:
            return []

    def locate(self, name: str) -> list[Path]:
        """Folders whose name matches, best tier only."""
        wanted = normalize_name(name)
        if not wanted:
            return []
        scored = {}
        for folder in self.candidates():
            have = normalize_name(folder.name)
            if not have:
                continue
            if have == wanted:
                score = 1.0
            elif len(wanted) >= 4 and (have.startswith(wanted) or wanted.startswith(have)):
                score = 0.9
            else:
                score = difflib.SequenceMatcher(None, wanted, have).ratio()
            if score >= MATCH_CUTOFF:
                scored[folder] = max(score, scored.get(folder, 0))
        if not scored:
            return []
        best = max(scored.values())
        return sorted(f for f, s in scored.items() if s >= best - 0.02)
