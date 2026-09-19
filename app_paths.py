"""Where Jarvis keeps things, the same whether run as `python main.py` or as
the packaged Jarvis.exe.

Persistent data (memories, projects, learning progress, logs, .env) lives in
%LOCALAPPDATA%\\Jarvis - never next to the code: a packaged build's folder
is replaced on every rebuild, so data kept there would be lost. Set
JARVIS_HOME to use another folder.

Read-only files that ship with the code (the HUD page, browser_tabs.ps1)
stay next to the code; PyInstaller copies them into the build.
"""
from __future__ import annotations

import logging
import os
import shutil
import sys
import uuid
from pathlib import Path

log = logging.getLogger("jarvis.data")

FROZEN = getattr(sys, "frozen", False)
CODE_DIR = Path(__file__).resolve().parent

# The folders each component used to keep its data in, inside the source tree.
COMPONENTS = ("memory", "projects", "islamic", "speech_coach")


def home() -> Path:
    configured = os.environ.get("JARVIS_HOME")
    if configured:
        return Path(configured).expanduser()
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / "Jarvis"


def data_dir(component: str) -> Path:
    return home() / component


def env_file() -> Path:
    return home() / ".env"


def log_file() -> Path:
    return home() / "jarvis.log"


_REDIRECT: Path | None | str = "unknown"


def redirected_home(recheck: bool = False) -> Path | None:
    """Where writes to home() really end up, if Windows redirects them.

    A program started from a packaged (MSIX) Windows app - e.g. a terminal
    inside the Claude desktop app - has its AppData writes silently moved to
    %LOCALAPPDATA%\\Packages\\<app>\\LocalCache. It still reads them back,
    so everything looks fine, but Jarvis started from the Desktop never sees
    them. Returns that private folder, or None when writes are real.
    """
    global _REDIRECT
    if _REDIRECT != "unknown" and not recheck:
        return _REDIRECT
    _REDIRECT = None
    base = os.environ.get("LOCALAPPDATA")
    if not base or os.environ.get("JARVIS_HOME"):
        return None
    packages = Path(base) / "Packages"
    if not packages.is_dir():
        return None
    target = home()
    try:
        target.mkdir(parents=True, exist_ok=True)
        probe = target / f".write-probe-{uuid.uuid4().hex}"
        probe.touch()
    except OSError:
        return None
    try:
        for private in packages.glob("*/LocalCache/Local/" + target.name):
            if (private / probe.name).exists():
                _REDIRECT = private
                return private
        return None
    finally:
        probe.unlink(missing_ok=True)

_warned = False


def warn_if_redirected(db_path) -> Path | None:
    """Shout before a redirected process opens Jarvis's live data.

    Opening one of these databases read-WRITE from such a process is not
    harmless: SQLite folds the write-ahead log into the database file, the
    result lands in the private copy, and the real file is left with a
    stranded log - which looks exactly like "my profiles are gone". That
    happened once; this is so it can never be silent again.
    """
    global _warned
    path = Path(str(db_path))
    if str(db_path) == ":memory:" or home() not in path.parents:
        return None
    private = redirected_home()
    if private is None:
        return None
    if not _warned:
        _warned = True
        log.warning(
            "This process's AppData writes are redirected to %s - it was started from "
            "a packaged app (the Claude desktop app, for one). Opening %s here writes "
            "to that private copy, and the Jarvis started from the Desktop will not "
            "see it. From there, open these databases read-only "
            "(sqlite3 'file:...?mode=ro') or run outside that app.",
            private, path.name)
    return private


def migrate_legacy_data(source_root: Path = CODE_DIR) -> list[str]:
    """Copy data created before this module existed (memory/data, ...) to
    the persistent folder - once, and only where the new folder is still
    empty. Originals are left in place. Run it while Jarvis is stopped:
    SQLite's -wal/-shm files are copied along with each database.
    """
    copied = []
    for component in COMPONENTS:
        legacy = source_root / component / "data"
        target = data_dir(component)
        if not legacy.is_dir() or not any(legacy.iterdir()):
            continue
        if target.exists() and any(target.iterdir()):
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(legacy, target, dirs_exist_ok=True)
        copied.append(component)
    return copied


if __name__ == "__main__":
    print(f"Jarvis data folder: {home()}")
    moved = migrate_legacy_data()
    print("Copied: " + ", ".join(moved) if moved else "Nothing to copy.")
