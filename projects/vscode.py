"""VSCodeController: open a folder or file in Visual Studio Code.

Launches Code.exe directly rather than the `code.cmd` shim: going through
cmd.exe would let a folder name containing & or ^ be interpreted as shell
syntax. If VS Code already has the folder open, it just comes to the front.
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path

logger = logging.getLogger("jarvis.projects.vscode")


def find_code_executable() -> Path | None:
    candidates = []
    shim = shutil.which("code")
    if shim:
        shim_path = Path(shim).resolve()
        # ...\Microsoft VS Code\bin\code(.cmd) -> ...\Microsoft VS Code\Code.exe
        candidates.append(shim_path.parent.parent / "Code.exe")
        if sys.platform != "win32":
            candidates.append(shim_path)
    local = os.environ.get("LOCALAPPDATA")
    if local:
        candidates.append(Path(local) / "Programs" / "Microsoft VS Code" / "Code.exe")
    for env in ("ProgramFiles", "ProgramFiles(x86)"):
        base = os.environ.get(env)
        if base:
            candidates.append(Path(base) / "Microsoft VS Code" / "Code.exe")
    candidates += [Path("/Applications/Visual Studio Code.app/Contents/Resources/app/bin/code"),
                   Path("/usr/bin/code"), Path("/snap/bin/code")]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


class VSCodeController:
    def __init__(self, executable: Path | None = None):
        self._executable = executable

    @property
    def executable(self) -> Path | None:
        if self._executable is None:
            self._executable = find_code_executable()
        return self._executable

    def is_available(self) -> bool:
        return self.executable is not None

    def open(self, path, line: int | None = None) -> tuple[bool, str]:
        exe = self.executable
        if exe is None:
            return False, "VS Code is not installed (or not where I can find it)."
        target = Path(path)
        if not target.exists():
            return False, f"{target} does not exist."
        args = [str(exe)]
        if line and target.is_file():
            args += ["--goto", f"{target}:{int(line)}"]
        else:
            args.append(str(target))
        flags = 0
        if sys.platform == "win32":
            flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        env = {k: v for k, v in os.environ.items() if k != "ELECTRON_RUN_AS_NODE"}
        try:
            subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, close_fds=True,
                             creationflags=flags, env=env)
        except OSError as exc:
            logger.warning("could not start VS Code: %s", exc)
            return False, f"Could not start VS Code: {exc}"
        return True, f"Opened {target.name} in VS Code."
