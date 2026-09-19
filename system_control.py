r"""Jarvis's hands on the computer itself: volume, screen brightness, files
and folders, power, media keys, battery, running apps and the clipboard.

All of it is Windows-only and local - nothing here talks to the network.

    from system_control import SystemController
    pc = SystemController()
    pc.set_volume(30)                      # percent
    pc.change_brightness(-20)              # relative, all monitors
    pc.create_folder(r"D:\Projects\Jarvis_Logs")
    pc.power("shutdown", seconds=60, confirm_token=...)

Anything that cannot be taken back asks first. Deleting, shutting down,
restarting and force-closing an app return `needs_confirmation` with a
`confirm_token`; nothing happens until the same call comes back with that
token (the pattern window_manager.py uses). Jarvis reads the question out
loud and only confirms after the user clearly says yes.

What this module deliberately does NOT do:

* It never deletes anything permanently when the Recycle Bin is available
  (send2trash); with the bin unavailable it says so instead of guessing.
* It refuses to touch Windows itself, Program Files, a drive root, the
  user's profile root and Jarvis's own data folder - an accident there is
  not recoverable by the person holding the microphone.
* It will not kill the processes Windows needs (csrss, winlogon, ...) or
  the Jarvis process it is running in.
* Closing an app with a window goes through window_manager.close_window
  first: that is the X button, so the app can still ask "save changes?".
  force_close here is the last resort and loses unsaved work.

The optional pieces degrade instead of failing: a machine with no battery
reports "no battery", a monitor that doesn't answer DDC/CI is named in the
result, and a missing library asks for the one pip install it needs.
"""
from __future__ import annotations

import hashlib
import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path

log = logging.getLogger("jarvis.system")

IS_WINDOWS = sys.platform == "win32"

# Timeouts for the small command-line tools below
COMMAND_TIMEOUT = 20
# How long to wait for a process to go away after asking it to stop
CLOSE_WAIT = 5
# Longest clipboard text read back out loud
CLIPBOARD_SPOKEN = 400
# Most entries listed from a folder in one go
LIST_LIMIT = 60

MEDIA_KEYS = {
    "playpause": "playpause", "play": "playpause", "pause": "playpause",
    "next": "nexttrack", "nexttrack": "nexttrack", "skip": "nexttrack",
    "previous": "prevtrack", "prevtrack": "prevtrack", "back": "prevtrack",
    "stop": "stop",
    "mute": "volumemute", "volumeup": "volumeup", "volumedown": "volumedown",
}

POWER_ACTIONS = ("shutdown", "restart", "lock", "sleep", "hibernate", "cancel")
# Shutdown and restart wait this long by default, so "no, stop!" still works
POWER_DELAY = 60

# Apps by the name they are spoken as -> what to start
APP_ALIASES = {
    "chrome": "chrome.exe", "google chrome": "chrome.exe",
    "edge": "msedge.exe", "microsoft edge": "msedge.exe",
    "firefox": "firefox.exe",
    "notepad": "notepad.exe", "wordpad": "write.exe",
    "calculator": "calc.exe", "calc": "calc.exe",
    "paint": "mspaint.exe",
    "explorer": "explorer.exe", "file explorer": "explorer.exe",
    "cmd": "cmd.exe", "command prompt": "cmd.exe",
    "powershell": "powershell.exe", "terminal": "wt.exe",
    "task manager": "taskmgr.exe",
    "settings": "ms-settings:", "control panel": "control.exe",
    "word": "winword.exe", "excel": "excel.exe", "powerpoint": "powerpnt.exe",
    "outlook": "outlook.exe", "teams": "ms-teams.exe",
    "vs code": "code.cmd", "vscode": "code.cmd", "visual studio code": "code.cmd",
    "spotify": "spotify.exe", "whatsapp": "whatsapp.exe",
}

# Never kill these: Windows falls over, or Jarvis kills itself
CRITICAL_PROCESSES = {
    "system", "system idle process", "registry", "memory compression",
    "csrss.exe", "wininit.exe", "winlogon.exe", "services.exe", "lsass.exe",
    "smss.exe", "svchost.exe", "dwm.exe", "fontdrvhost.exe", "audiodg.exe",
    "ctfmon.exe", "runtimebroker.exe", "sihost.exe", "shellexperiencehost.exe",
}


def _env_paths(*names: str) -> list[Path]:
    out = []
    for name in names:
        value = os.environ.get(name)
        if value:
            try:
                out.append(Path(value).resolve())
            except OSError:
                pass
    return out


def _off_limits() -> list[Path]:
    """Windows itself and Jarvis's own files - not even what is inside."""
    import app_paths
    paths = _env_paths("SystemRoot", "ProgramFiles", "ProgramFiles(x86)", "ProgramData")
    paths.append(app_paths.home().resolve())    # memories, profiles, WhatsApp login
    paths.append(app_paths.CODE_DIR.resolve())  # and the code itself
    return paths


def _folders_to_keep() -> list[Path]:
    """The user's own roots: files inside are theirs, the folder stays."""
    paths = _env_paths("USERPROFILE", "APPDATA", "LOCALAPPDATA")
    try:
        paths.append(Path.home().resolve())
    except OSError:
        pass
    return paths


class SystemControlError(Exception):
    """A failure Jarvis can explain: status + a line to say out loud.

    status: unsupported, missing_library, bad_path, no_drive, protected,
    not_found, permission, busy, failed, needs_library.
    """

    def __init__(self, status: str, message: str, **details):
        super().__init__(message)
        self.status = status
        self.message = message
        self.details = details

    def as_result(self) -> dict:
        return {"ok": False, "status": self.status, "message": self.message, **self.details}


def _ok(message: str, **fields) -> dict:
    return {"ok": True, "status": "ok", "message": message, **fields}


def _token(action: str, detail: str) -> str:
    """Short handle for one exact pending action - see the module docstring."""
    return hashlib.sha1(f"{action}|{detail}".encode("utf-8")).hexdigest()[:8]


def _needs_confirmation(kind: str, detail: str, question: str, **fields) -> dict:
    """Nothing done yet - here is the question and the token to come back with.

    The parameters are named `kind`/`detail` rather than `action`, which is
    also a field some callers pass back to Jarvis.
    """
    return {"ok": False, "needs_confirmation": True,
            "confirm_token": _token(kind, detail), "message": question, **fields}


def _percent(value, what: str = "value") -> int:
    try:
        number = int(round(float(value)))
    except (TypeError, ValueError):
        raise SystemControlError("failed", f"'{value}' is not a {what} I understand.") from None
    return max(0, min(100, number))


def _require_windows() -> None:
    if not IS_WINDOWS:
        raise SystemControlError("unsupported", "This only works on Windows.")


def _import(module: str, package: str):
    try:
        return __import__(module, fromlist=["_"])
    except ImportError:
        raise SystemControlError(
            "missing_library",
            f"The {package} library isn't installed - run: pip install {package}") from None


class SystemController:
    """Local machine controls. Every method returns a dict with a `message`.

    Safe to call from any thread: COM is initialised per call (Jarvis runs
    tools in `asyncio.to_thread`, so calls arrive on different threads), and
    nothing is cached between calls.
    """

    # ---- volume ---------------------------------------------------------

    def _endpoint(self):
        """The default speaker's volume interface, inside a COM apartment."""
        _require_windows()
        _import("comtypes", "comtypes")
        pycaw = _import("pycaw.utils", "pycaw")
        try:
            device = pycaw.AudioUtilities.GetSpeakers()
            return device, device.EndpointVolume
        except Exception as exc:
            log.warning("volume: %s: %s", type(exc).__name__, exc)
            raise SystemControlError(
                "failed", "I couldn't reach the speakers - is an audio device connected?"
            ) from None

    def _com(self):
        """CoInitialize for this thread; pycaw needs it off the main thread."""
        try:
            import pythoncom
        except ImportError:
            return None
        try:
            pythoncom.CoInitialize()
        except Exception:      # already initialised in this thread
            return None
        return pythoncom

    def volume_status(self) -> dict:
        """Current master volume and mute state."""
        com = self._com()
        try:
            device, endpoint = self._endpoint()
            level = round(endpoint.GetMasterVolumeLevelScalar() * 100)
            muted = bool(endpoint.GetMute())
            name = getattr(device, "FriendlyName", "the speakers")
            return _ok(f"Volume is {level} percent" + (" and muted." if muted else "."),
                       level=level, muted=muted, device=name)
        finally:
            if com:
                com.CoUninitialize()

    def set_volume(self, percent) -> dict:
        """Set master volume to an exact percentage."""
        target = _percent(percent, "volume")
        com = self._com()
        try:
            _, endpoint = self._endpoint()
            endpoint.SetMasterVolumeLevelScalar(target / 100, None)
            if target and endpoint.GetMute():
                endpoint.SetMute(0, None)      # a volume request means "let me hear it"
            log.info("volume -> %d%%", target)
            return _ok(f"Volume set to {target} percent.", level=target, muted=False)
        finally:
            if com:
                com.CoUninitialize()

    def change_volume(self, delta) -> dict:
        """Raise (+) or lower (-) the volume by a number of percent."""
        try:
            step = int(round(float(delta)))
        except (TypeError, ValueError):
            raise SystemControlError("failed", f"'{delta}' is not a number of percent.") from None
        com = self._com()
        try:
            _, endpoint = self._endpoint()
            now = round(endpoint.GetMasterVolumeLevelScalar() * 100)
            target = max(0, min(100, now + step))
            endpoint.SetMasterVolumeLevelScalar(target / 100, None)
            if target and endpoint.GetMute():
                endpoint.SetMute(0, None)
            log.info("volume %d%% -> %d%%", now, target)
            word = "up" if step >= 0 else "down"
            return _ok(f"Volume {word} to {target} percent.", level=target, previous=now)
        finally:
            if com:
                com.CoUninitialize()

    def mute(self, state: str = "toggle") -> dict:
        """state: "on" (mute), "off" (unmute) or "toggle"."""
        wanted = (state or "toggle").strip().lower()
        com = self._com()
        try:
            _, endpoint = self._endpoint()
            muted = bool(endpoint.GetMute())
            if wanted in ("on", "mute", "true", "yes"):
                target = True
            elif wanted in ("off", "unmute", "false", "no"):
                target = False
            else:
                target = not muted
            endpoint.SetMute(1 if target else 0, None)
            level = round(endpoint.GetMasterVolumeLevelScalar() * 100)
            return _ok("Sound is muted." if target else f"Sound is back on at {level} percent.",
                       muted=target, level=level)
        finally:
            if com:
                com.CoUninitialize()

    # ---- brightness -----------------------------------------------------

    @staticmethod
    def _sbc():
        return _import("screen_brightness_control", "screen-brightness-control")

    def brightness_status(self) -> dict:
        """Brightness of every monitor that answers."""
        sbc = self._sbc()
        try:
            monitors = sbc.list_monitors()
            levels = sbc.get_brightness()
        except Exception as exc:
            log.warning("brightness: %s: %s", type(exc).__name__, exc)
            raise SystemControlError(
                "failed", "No monitor would tell me its brightness.") from None
        if not levels:
            raise SystemControlError(
                "not_found", "No monitor here supports brightness control.")
        pairs = list(zip(monitors or [f"display {i}" for i in range(len(levels))], levels))
        spoken = ", ".join(f"{name} {level} percent" for name, level in pairs)
        return _ok(f"Brightness: {spoken}.",
                   displays=[{"name": n, "level": v} for n, v in pairs],
                   level=levels[0])

    def set_brightness(self, percent, display: str | None = None, smooth: bool = True) -> dict:
        """Set brightness. `display` picks one monitor, otherwise all of them.

        A monitor that refuses (many external ones need DDC/CI enabled) is
        named in the result instead of failing the whole call.
        """
        target = _percent(percent, "brightness")
        sbc = self._sbc()
        names = self._displays(sbc, display)

        done, refused = [], []
        for name in names:
            try:
                if smooth:
                    sbc.fade_brightness(target, display=name, interval=0.01, increment=5)
                else:
                    sbc.set_brightness(target, display=name)
                done.append(name)
            except Exception as exc:
                log.info("brightness %s: %s: %s", name, type(exc).__name__, exc)
                refused.append(name)

        if not done:
            raise SystemControlError(
                "failed",
                f"{', '.join(refused)} wouldn't change brightness - an external monitor "
                "usually needs DDC/CI turned on in its own menu.")
        message = f"Brightness set to {target} percent"
        message += f" on {', '.join(done)}." if len(names) > 1 else "."
        if refused:
            message += f" {', '.join(refused)} wouldn't listen."
        log.info("brightness -> %d%% on %s", target, ", ".join(done))
        return _ok(message, level=target, changed=done, refused=refused)

    def change_brightness(self, delta, display: str | None = None) -> dict:
        """Raise (+) or lower (-) brightness by a number of percent."""
        try:
            step = int(round(float(delta)))
        except (TypeError, ValueError):
            raise SystemControlError("failed", f"'{delta}' is not a number of percent.") from None
        sbc = self._sbc()
        names = self._displays(sbc, display)
        try:
            now = sbc.get_brightness(display=names[0])
            now = now[0] if isinstance(now, list) else now
        except Exception:
            raise SystemControlError(
                "failed", "I couldn't read the current brightness.") from None
        return self.set_brightness(max(0, min(100, now + step)), display=display)

    @staticmethod
    def _displays(sbc, display: str | None) -> list[str]:
        try:
            monitors = sbc.list_monitors()
        except Exception:
            monitors = []
        if not monitors:
            raise SystemControlError(
                "not_found", "I can't find a monitor that supports brightness control.")
        if not display:
            return monitors
        wanted = display.strip().casefold()
        matches = [m for m in monitors if wanted in m.casefold()]
        if not matches:
            raise SystemControlError(
                "not_found",
                f"No monitor called '{display}'. I can see: {', '.join(monitors)}.")
        return matches

    # ---- files and folders ----------------------------------------------

    @staticmethod
    def _resolve(path: str | Path, must_exist: bool = False) -> Path:
        """A real absolute path, with a clear error when the drive is wrong."""
        raw = str(path or "").strip().strip('"')
        if not raw:
            raise SystemControlError("bad_path", "Which folder or file do you mean?")
        if any(ch in raw for ch in "*?"):
            raise SystemControlError("bad_path", "I don't do wildcards - name one folder or file.")
        try:
            resolved = Path(os.path.expandvars(raw)).expanduser()
            resolved = (Path.cwd() / resolved).resolve() if not resolved.is_absolute() \
                else resolved.resolve()
        except OSError as exc:
            raise SystemControlError("bad_path", f"'{raw}' isn't a path I can use ({exc}).") from None

        drive = resolved.drive
        if IS_WINDOWS and drive and not Path(drive + "\\").exists():
            letters = [f"{d}:" for d in "CDEFGHIJKLMNOPQRSTUVWXYZ"
                       if Path(f"{d}:\\").exists()]
            raise SystemControlError(
                "no_drive",
                f"There is no {drive} drive on this computer. I can see {', '.join(letters)}.")
        if must_exist and not resolved.exists():
            raise SystemControlError("not_found", f"{resolved} doesn't exist.")
        return resolved

    @classmethod
    def _guard(cls, path: Path, what: str) -> None:
        """Refuse the places where a mistake cannot be undone."""
        if path.parent == path:          # C:\ , D:\ ...
            raise SystemControlError(
                "protected", f"I won't {what} a whole drive ({path}).")
        for root in _off_limits():
            if path == root or root in path.parents:
                raise SystemControlError(
                    "protected",
                    f"{path} is inside {root.name} - Windows's or Jarvis's own files. "
                    f"I won't {what} it.")
        if path in _folders_to_keep():
            raise SystemControlError(
                "protected", f"{path} is one of your main folders - I won't {what} it.")

    def create_folder(self, path: str) -> dict:
        """Create a folder (with any missing parents)."""
        target = self._resolve(path)
        if target.exists():
            if target.is_dir():
                return _ok(f"{target} is already there.", path=str(target), created=False)
            raise SystemControlError("bad_path", f"{target} is a file, not a folder.")
        try:
            target.mkdir(parents=True)
        except PermissionError:
            raise SystemControlError(
                "permission", f"Windows wouldn't let me create {target}.") from None
        except OSError as exc:
            raise SystemControlError("failed", f"I couldn't create {target}: {exc}") from None
        log.info("created folder %s", target)
        return _ok(f"Created {target}.", path=str(target), created=True)

    def list_folder(self, path: str, limit: int = LIST_LIMIT) -> dict:
        """What is in a folder: folders first, then files."""
        target = self._resolve(path, must_exist=True)
        if not target.is_dir():
            return _ok(f"{target.name} is a file of {target.stat().st_size} bytes.",
                       path=str(target), entries=[])
        try:
            entries = sorted(target.iterdir(), key=lambda p: (p.is_file(), p.name.casefold()))
        except PermissionError:
            raise SystemControlError(
                "permission", f"Windows wouldn't let me read {target}.") from None
        folders = [e.name for e in entries if e.is_dir()]
        files = [e.name for e in entries if e.is_file()]
        shown = [{"name": e.name, "kind": "folder" if e.is_dir() else "file",
                  "size": e.stat().st_size if e.is_file() else None}
                 for e in entries[:limit]]
        if not entries:
            message = f"{target} is empty."
        else:
            message = (f"{target} has {len(folders)} folders and {len(files)} files. "
                       + ", ".join(e.name for e in entries[:8])
                       + (" and more." if len(entries) > 8 else ""))
        return _ok(message, path=str(target), entries=shown,
                   folders=len(folders), files=len(files))

    def move(self, source: str, destination: str) -> dict:
        """Move (or rename) a file or folder."""
        src = self._resolve(source, must_exist=True)
        self._guard(src, "move")
        dst = self._resolve(destination)
        if dst.is_dir():
            dst = dst / src.name
        if dst.exists():
            raise SystemControlError("failed", f"{dst} already exists - I won't overwrite it.")
        dst.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.move(str(src), str(dst))
        except PermissionError:
            raise SystemControlError(
                "permission", f"{src.name} is in use or protected, so I couldn't move it.") from None
        except OSError as exc:
            raise SystemControlError("failed", f"I couldn't move {src.name}: {exc}") from None
        log.info("moved %s -> %s", src, dst)
        return _ok(f"Moved {src.name} to {dst}.", source=str(src), path=str(dst))

    def delete(self, path: str, confirm_token: str | None = None) -> dict:
        """Delete a file or folder - to the Recycle Bin, and only after a yes."""
        target = self._resolve(path, must_exist=True)
        self._guard(target, "delete")

        if target.is_dir():
            inside = sum(1 for _ in target.rglob("*"))
            what = f"the folder {target} with {inside} items inside" if inside else \
                   f"the empty folder {target}"
        else:
            what = f"the file {target}"

        if confirm_token != _token("delete", str(target)):
            return _needs_confirmation(
                "delete", str(target),
                f"Delete {what}? It goes to the Recycle Bin.", path=str(target))

        try:
            send2trash = __import__("send2trash", fromlist=["_"])
        except ImportError:
            raise SystemControlError(
                "needs_library",
                "I only delete to the Recycle Bin, and that needs send2trash: "
                "pip install send2trash. Nothing was deleted.") from None
        try:
            send2trash.send2trash(str(target))
        except PermissionError:
            raise SystemControlError(
                "permission", f"{target.name} is in use, so I couldn't delete it.") from None
        except Exception as exc:
            raise SystemControlError("failed", f"I couldn't delete {target.name}: {exc}") from None
        log.info("deleted (recycle bin) %s", target)
        return _ok(f"{target.name} is in the Recycle Bin.", path=str(target))

    # ---- power ----------------------------------------------------------

    def power(self, action: str, seconds: int = POWER_DELAY,
              confirm_token: str | None = None) -> dict:
        """shutdown | restart | lock | sleep | hibernate | cancel."""
        _require_windows()
        wanted = (action or "").strip().lower()
        if wanted not in POWER_ACTIONS:
            raise SystemControlError(
                "failed", f"I can shutdown, restart, lock, sleep, hibernate or cancel - not '{action}'.")

        if wanted == "cancel":
            done = self._run(["shutdown", "/a"])
            if done.returncode != 0:
                return _ok("There was no shutdown waiting to cancel.", cancelled=False)
            log.info("shutdown cancelled")
            return _ok("Shutdown cancelled.", cancelled=True)

        if wanted == "lock":
            self._run(["rundll32.exe", "user32.dll,LockWorkStation"], check=True)
            return _ok("Locking the computer.")

        if wanted == "sleep":
            # 0,1,0 = suspend, forced, wake events allowed. With hibernation
            # enabled Windows hibernates instead - it decides, not us.
            self._run(["rundll32.exe", "powrprof.dll,SetSuspendState", "0,1,0"], check=True)
            return _ok("Going to sleep.")

        delay = max(0, int(seconds or 0))
        if wanted == "hibernate":
            if confirm_token != _token("power", "hibernate"):
                return _needs_confirmation("power", "hibernate", "Hibernate the computer?")
            self._run(["shutdown", "/h"], check=True)
            return _ok("Hibernating.")

        detail = f"{wanted}:{delay}"
        if confirm_token != _token("power", detail):
            when = "now" if delay == 0 else f"in {delay} seconds"
            return _needs_confirmation(
                "power", detail,
                f"{'Shut down' if wanted == 'shutdown' else 'Restart'} the computer {when}? "
                "Save your work first - say 'cancel shutdown' to stop it.",
                action=wanted, seconds=delay)

        flag = "/s" if wanted == "shutdown" else "/r"
        self._run(["shutdown", flag, "/t", str(delay)], check=True)
        log.info("%s in %ds", wanted, delay)
        word = "Shutting down" if wanted == "shutdown" else "Restarting"
        return _ok(f"{word} in {delay} seconds. Say 'cancel shutdown' if you change your mind.",
                   action=wanted, seconds=delay)

    @staticmethod
    def _run(command: list[str], check: bool = False) -> subprocess.CompletedProcess:
        try:
            done = subprocess.run(command, capture_output=True, text=True,
                                  timeout=COMMAND_TIMEOUT)
        except FileNotFoundError:
            raise SystemControlError("failed", f"{command[0]} isn't available here.") from None
        except subprocess.TimeoutExpired:
            raise SystemControlError("failed", f"{command[0]} didn't finish in time.") from None
        if check and done.returncode != 0:
            detail = (done.stderr or done.stdout or "").strip().splitlines()
            raise SystemControlError(
                "failed", f"Windows refused: {detail[0] if detail else done.returncode}")
        return done

    # ---- media keys -----------------------------------------------------

    def media(self, action: str) -> dict:
        """Play/pause, next or previous track - the keyboard's media keys.

        These go to whatever app owns media playback (Spotify, YouTube in a
        browser, the media player), exactly like the keys on a keyboard.
        """
        key = MEDIA_KEYS.get((action or "").strip().lower().replace(" ", ""))
        if key is None:
            raise SystemControlError(
                "failed", f"I know play, pause, next and previous - not '{action}'.")
        pyautogui = _import("pyautogui", "pyautogui")
        try:
            pyautogui.press(key)
        except Exception as exc:
            raise SystemControlError("failed", f"The media key didn't go through: {exc}") from None
        spoken = {"playpause": "Play/pause.", "nexttrack": "Next track.",
                  "prevtrack": "Previous track.", "stop": "Stopped.",
                  "volumemute": "Mute key pressed."}.get(key, f"{key} pressed.")
        return _ok(spoken, key=key)

    # ---- battery --------------------------------------------------------

    def battery(self) -> dict:
        """Charge, and whether it is plugged in."""
        psutil = _import("psutil", "psutil")
        state = psutil.sensors_battery()
        if state is None:
            return _ok("This computer has no battery - it runs on mains power.",
                       has_battery=False)
        percent = round(state.percent)
        plugged = bool(state.power_plugged)
        left = ""
        if not plugged and state.secsleft and state.secsleft > 0:
            hours, minutes = divmod(int(state.secsleft) // 60, 60)
            left = f", about {hours} hours {minutes} minutes left" if hours else \
                   f", about {minutes} minutes left"
        message = (f"Battery is at {percent} percent"
                   + (" and charging." if plugged else f"{left}.")
                   + (" Running low." if percent <= 20 and not plugged else ""))
        return _ok(message, percent=percent, plugged=plugged, has_battery=True,
                   seconds_left=state.secsleft if state.secsleft and state.secsleft > 0 else None)

    # ---- applications ---------------------------------------------------

    def open_app(self, name: str) -> dict:
        """Start an app by the name it is spoken as ("open Notepad")."""
        _require_windows()
        wanted = " ".join((name or "").strip().casefold().split())
        if not wanted:
            raise SystemControlError("failed", "Which app should I open?")
        target = APP_ALIASES.get(wanted)
        if target is None:
            target = wanted if wanted.endswith((".exe", ".cmd", ".bat")) else wanted + ".exe"
            if shutil.which(target) is None:
                found = self._start_menu(wanted)
                if not found:
                    raise SystemControlError(
                        "not_found",
                        f"I couldn't find an app called '{name}' to open.")
                target = str(found)
        try:
            if str(target).endswith(":") or str(target).startswith("ms-"):
                os.startfile(target)                      # ms-settings: and friends
            elif Path(str(target)).suffix.casefold() == ".lnk":
                os.startfile(str(target))
            else:
                subprocess.Popen([str(target)], shell=False,
                                 creationflags=getattr(subprocess, "DETACHED_PROCESS", 0))
        except FileNotFoundError:
            raise SystemControlError("not_found", f"'{name}' isn't installed here.") from None
        except OSError as exc:
            raise SystemControlError("failed", f"I couldn't open {name}: {exc}") from None
        log.info("opened %s", target)
        return _ok(f"Opening {name}.", app=name, target=str(target))

    @staticmethod
    def _start_menu(wanted: str) -> Path | None:
        """Look for a shortcut with that name in the Start menu."""
        roots = [Path(os.environ.get(var, "")) / "Microsoft" / "Windows" / "Start Menu"
                 for var in ("APPDATA", "ProgramData")]
        best = None
        for root in roots:
            if not root.is_dir():
                continue
            for link in root.rglob("*.lnk"):
                stem = link.stem.casefold()
                if stem == wanted:
                    return link
                if best is None and wanted in stem:
                    best = link
        return best

    def running_apps(self, limit: int = 15) -> dict:
        """The heaviest running apps, by memory - one line per app."""
        psutil = _import("psutil", "psutil")
        totals: dict[str, dict] = {}
        for proc in psutil.process_iter(["name", "memory_info"]):
            try:
                name = proc.info["name"]
                if not name or name.casefold() in CRITICAL_PROCESSES:
                    continue
                memory = getattr(proc.info["memory_info"], "rss", 0)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
            entry = totals.setdefault(name, {"name": name, "count": 0, "memory_mb": 0})
            entry["count"] += 1
            entry["memory_mb"] += round(memory / (1024 * 1024))
        apps = sorted(totals.values(), key=lambda a: a["memory_mb"], reverse=True)[:limit]
        spoken = ", ".join(a["name"].removesuffix(".exe") for a in apps[:6])
        return _ok(f"{len(totals)} apps are running. The biggest: {spoken}.",
                   apps=apps, total=len(totals))

    def force_close_app(self, name: str, confirm_token: str | None = None) -> dict:
        """Kill every process of an app. Last resort - unsaved work is lost.

        Prefer window_manager.close_window: that is the X button, so the app
        can ask to save first.
        """
        psutil = _import("psutil", "psutil")
        wanted = " ".join((name or "").strip().casefold().split())
        if not wanted:
            raise SystemControlError("failed", "Which app should I close?")
        alias = APP_ALIASES.get(wanted, wanted)
        needle = Path(str(alias)).stem.casefold()

        victims = []
        for proc in psutil.process_iter(["name", "pid"]):
            try:
                pname = (proc.info["name"] or "")
                stem = Path(pname).stem.casefold()
                if needle and (stem == needle or needle in stem):
                    if pname.casefold() in CRITICAL_PROCESSES:
                        raise SystemControlError(
                            "protected", f"{pname} is part of Windows - I won't close it.")
                    if proc.info["pid"] == os.getpid():
                        continue                      # never Jarvis itself
                    victims.append(proc)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        if not victims:
            raise SystemControlError("not_found", f"Nothing called '{name}' is running.")

        names = sorted({p.info["name"] for p in victims})
        detail = ",".join(names)
        if confirm_token != _token("force_close", detail):
            return _needs_confirmation(
                "force_close", detail,
                f"Force-close {len(victims)} {names[0]} "
                f"{'process' if len(victims) == 1 else 'processes'}? "
                "Anything unsaved in it is lost.",
                app=names[0], count=len(victims))

        for proc in victims:
            try:
                proc.terminate()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        gone, alive = psutil.wait_procs(victims, timeout=CLOSE_WAIT)
        for proc in alive:                      # it ignored the polite ask
            try:
                proc.kill()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        log.info("force closed %s (%d processes)", names[0], len(gone) + len(alive))
        left = len(psutil.wait_procs(alive, timeout=2)[1])
        if left:
            raise SystemControlError(
                "permission",
                f"{left} {names[0]} processes wouldn't close - they may need administrator rights.")
        return _ok(f"Closed {names[0]}.", app=names[0], closed=len(victims))

    # ---- clipboard ------------------------------------------------------

    def clipboard_read(self) -> dict:
        """What is on the clipboard right now."""
        pyperclip = _import("pyperclip", "pyperclip")
        try:
            text = pyperclip.paste() or ""
        except Exception as exc:
            raise SystemControlError("failed", f"I couldn't read the clipboard: {exc}") from None
        if not text.strip():
            return _ok("The clipboard is empty.", text="", length=0)
        spoken = text if len(text) <= CLIPBOARD_SPOKEN else text[:CLIPBOARD_SPOKEN] + "..."
        return _ok(f"The clipboard has: {spoken}", text=text, length=len(text))

    def clipboard_write(self, text: str) -> dict:
        """Put text on the clipboard, ready to paste."""
        if text is None or not str(text).strip():
            raise SystemControlError("failed", "What should I put on the clipboard?")
        pyperclip = _import("pyperclip", "pyperclip")
        try:
            pyperclip.copy(str(text))
        except Exception as exc:
            raise SystemControlError("failed", f"I couldn't write to the clipboard: {exc}") from None
        return _ok("Copied - ready to paste.", length=len(str(text)))

    # ---- one look at everything ----------------------------------------

    def status(self) -> dict:
        """Battery, volume and brightness in one line, for "how's my PC?"."""
        parts, details = [], {}
        for key, call in (("battery", self.battery),
                          ("volume", self.volume_status),
                          ("brightness", self.brightness_status)):
            try:
                result = call()
                details[key] = result
                parts.append(result["message"].rstrip("."))
            except SystemControlError as exc:
                details[key] = exc.as_result()
        return _ok(". ".join(parts) + "." if parts else "I couldn't read anything.", **details)


# --- Jarvis voice tools ------------------------------------------------------

_controller: SystemController | None = None


def controller() -> SystemController:
    global _controller
    if _controller is None:
        _controller = SystemController()
    return _controller


def _guarded(call, *args, **kwargs) -> dict:
    try:
        return call(*args, **kwargs)
    except SystemControlError as exc:
        return exc.as_result()
    except Exception as exc:                      # a tool must never take Jarvis down
        log.exception("system control failed")
        return {"ok": False, "status": "failed",
                "message": f"Something went wrong: {type(exc).__name__}."}


def volume_tool(args: dict) -> dict:
    pc = controller()
    action = (args.get("action") or "status").strip().lower()
    if action in ("set", "level"):
        return _guarded(pc.set_volume, args.get("percent"))
    if action in ("up", "increase", "louder"):
        return _guarded(pc.change_volume, abs(float(args.get("percent") or 10)))
    if action in ("down", "decrease", "quieter"):
        return _guarded(pc.change_volume, -abs(float(args.get("percent") or 10)))
    if action in ("mute", "unmute", "toggle_mute"):
        return _guarded(pc.mute, {"mute": "on", "unmute": "off"}.get(action, "toggle"))
    return _guarded(pc.volume_status)


def brightness_tool(args: dict) -> dict:
    pc = controller()
    action = (args.get("action") or "status").strip().lower()
    display = args.get("display") or None
    if action in ("set", "level"):
        return _guarded(pc.set_brightness, args.get("percent"), display)
    if action in ("up", "increase", "brighter"):
        return _guarded(pc.change_brightness, abs(float(args.get("percent") or 10)), display)
    if action in ("down", "decrease", "dimmer"):
        return _guarded(pc.change_brightness, -abs(float(args.get("percent") or 10)), display)
    return _guarded(pc.brightness_status)


def files_tool(args: dict) -> dict:
    pc = controller()
    action = (args.get("action") or "").strip().lower()
    path = args.get("path") or ""
    if action in ("create_folder", "create", "mkdir"):
        return _guarded(pc.create_folder, path)
    if action in ("list", "show"):
        return _guarded(pc.list_folder, path)
    if action == "move":
        return _guarded(pc.move, path, args.get("destination") or "")
    if action == "delete":
        return _guarded(pc.delete, path, args.get("confirm_token"))
    return {"ok": False, "status": "failed",
            "message": "action: create_folder, list, move ya delete."}


def power_tool(args: dict) -> dict:
    return _guarded(controller().power,
                    args.get("action") or "", int(args.get("seconds") or POWER_DELAY),
                    args.get("confirm_token"))


def apps_tool(args: dict) -> dict:
    pc = controller()
    action = (args.get("action") or "list").strip().lower()
    if action in ("open", "start", "launch"):
        return _guarded(pc.open_app, args.get("name") or "")
    if action in ("force_close", "kill", "close"):
        return _guarded(pc.force_close_app, args.get("name") or "", args.get("confirm_token"))
    return _guarded(pc.running_apps)


def clipboard_tool(args: dict) -> dict:
    pc = controller()
    if (args.get("action") or "read").strip().lower() in ("write", "copy", "set"):
        return _guarded(pc.clipboard_write, args.get("text") or "")
    return _guarded(pc.clipboard_read)


VOICE_TOOLS = {
    "control_volume": volume_tool,
    "control_brightness": brightness_tool,
    "manage_files": files_tool,
    "power_control": power_tool,
    "manage_apps": apps_tool,
    "clipboard": clipboard_tool,
    "media_control": lambda args: _guarded(controller().media, args.get("action") or "playpause"),
    "system_status": lambda _args: _guarded(controller().status),
}


# --- CLI ----------------------------------------------------------------------

def _cli(argv=None) -> int:
    """Try any of it without Jarvis:  python system_control.py status"""
    import argparse
    import json

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    parser = argparse.ArgumentParser(description="Jarvis system controls")
    parser.add_argument("tool", choices=sorted(VOICE_TOOLS))
    parser.add_argument("pairs", nargs="*", help="key=value, e.g. action=set percent=30")
    args = parser.parse_args(argv)

    call_args = {}
    for pair in args.pairs:
        key, _, value = pair.partition("=")
        call_args[key] = value
    result = VOICE_TOOLS[args.tool](call_args)
    print(result.get("message", ""))
    print(json.dumps({k: v for k, v in result.items() if k != "message"},
                     ensure_ascii=False, indent=1, default=str))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(_cli())
