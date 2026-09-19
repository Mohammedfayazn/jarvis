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

import os
import shutil
import sys
from pathlib import Path

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
