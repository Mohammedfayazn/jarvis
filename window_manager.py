"""Jarvis ka window manager - Windows par chal rahi apps ko control karna.

Public API (sab dict lautate hain jisme `message` hota hai - wahi Jarvis
bolta hai):

    list_open_windows()                      -> list[dict]  title/process/hwnd
    close_window(name, confirm_token=None)
    close_all_windows(app, confirm_token=None)
    close_all_windows_except_current(confirm_token=None)
    close_all_browser_windows() / close_all_outlook_windows() /
        close_all_excel_windows()
    focus_window(name)
    minimize_window(name)
    maximize_window(name)
    run_command("close chrome")              # text command -> upar wale

Andar ka dhaancha:

    naam ("close visual studio")
        |  _normalize   -> "visual studio"
        v
    resolve_app  -- alias table (APPS) -> process names (devenv.exe)
        |            na mile toh fuzzy: title + process par difflib score
        v
    find_windows -- _enum_windows: sirf woh windows jo Alt+Tab me dikhti
        |            (visible, titled, owner-less, tool/cloaked nahi)
        v
    _protected   -- Jarvis ka apna terminal/VS Code, HUD, Windows shell -
        |            inhe kabhi band nahi karte
        v
    _close_windows
        |  unsaved?  title markers (*, ●) + Office COM (Workbook.Saved...)
        |  multiple? ek naam, kai windows
        |  -> haan toh needs_confirmation + confirm_token, kuch band nahi
        v
    WM_CLOSE  -- X button wala band, force-kill kabhi nahi. Office khud
        |        "Save changes?" poochhta hai - detection chooke toh bhi.
        v
    _wait_closed -- sach me gayi ya "Save?" par atki, gin kar batao

Kyun aise:

* WM_CLOSE, kill nahi: unsaved kaam ka asli bachaav app ka apna "Save
  changes?" dialog hai. Hamari unsaved detection (title + COM) sirf pehle
  poochhne ke liye hai - woh chook sakti hai, app ka dialog nahi.
* confirm_token: needs_confirmation par ek token milta hai jo band hone
  wali windows ki pehchaan se banta hai. Model use bana nahi sakta, toh
  bina user se poochhe bada band ho hi nahi sakta. Beech me windows badlin
  toh token nahi milega aur dobara poochha jayega.
* "current" window: awaaz se hukum dete waqt saamne aksar Jarvis ka HUD ya
  terminal hota hai. Woh "current" nahi - current woh hai jo unke peeche
  sabse upar hai. Confirmation me naam se bataya jata hai kaunsi bachegi.
* Focus: Windows background process ko window aage laane nahi deta
  (foreground lock). Pehle AttachThreadInput, phir pyautogui se ek Alt -
  wahi jo Windows khud "user ne kuch dabaya" maanta hai.
"""

from __future__ import annotations

import ctypes
import difflib
import hashlib
import os
import re
import sys
import time
from ctypes import wintypes
from dataclasses import dataclass

IS_WINDOWS = sys.platform == "win32"

if IS_WINDOWS:
    import psutil
    import pyautogui
    import pygetwindow
    import pythoncom
    import pywintypes
    import win32api
    import win32com.client
    import win32con
    import win32gui
    import win32process

    _dwmapi = ctypes.WinDLL("dwmapi")
    _dwmapi.DwmGetWindowAttribute.argtypes = [
        wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
    ]

# Jarvis ke HUD page ka <title> isi par khatam hota hai
HUD_MARKER = "J.A.R.V.I.S."

# Windows ki apni "windows" - desktop, taskbar. Progman ko WM_CLOSE bhejo toh
# "Shut Down Windows" dialog khul jata hai.
SHELL_CLASSES = {"Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd"}

DWMWA_CLOAKED = 14

# Fuzzy match ka kam se kam score (0..1)
MATCH_THRESHOLD = 0.75

# WM_CLOSE ke baad itni der tak dekhte hain ki window sach me gayi ya nahi
CLOSE_WAIT_SECONDS = 4.0


# --- App catalogue ----------------------------------------------------------

@dataclass(frozen=True)
class App:
    key: str
    label: str                    # bolne layak naam: "Excel", "VS Code"
    processes: frozenset[str]     # lowercase exe names
    title_pattern: str = ""       # process ke alawa title se bhi pehchan (PDF)


BROWSER_PROCESSES = frozenset({
    "chrome.exe", "msedge.exe", "firefox.exe", "brave.exe", "opera.exe",
    "vivaldi.exe",
})


def _app(key, label, *procs, title_pattern=""):
    return App(key, label, frozenset(procs), title_pattern)


APPS: dict[str, App] = {a.key: a for a in (
    _app("chrome", "Chrome", "chrome.exe"),
    _app("edge", "Edge", "msedge.exe"),
    _app("firefox", "Firefox", "firefox.exe"),
    _app("brave", "Brave", "brave.exe"),
    _app("opera", "Opera", "opera.exe"),
    App("browser", "browser", BROWSER_PROCESSES),
    _app("excel", "Excel", "excel.exe"),
    _app("word", "Word", "winword.exe"),
    _app("powerpoint", "PowerPoint", "powerpnt.exe"),
    _app("outlook", "Outlook", "outlook.exe", "olk.exe"),   # classic + new
    _app("onenote", "OneNote", "onenote.exe"),
    _app("teams", "Teams", "ms-teams.exe", "teams.exe"),
    _app("vscode", "VS Code", "code.exe"),
    _app("visualstudio", "Visual Studio", "devenv.exe"),
    _app("pdf", "PDF", "acrobat.exe", "acrord32.exe", "sumatrapdf.exe",
         "foxitpdfreader.exe", title_pattern=r"\.pdf\b"),
    _app("explorer", "File Explorer", "explorer.exe"),
    _app("notepad", "Notepad", "notepad.exe"),
    _app("terminal", "Terminal", "windowsterminal.exe"),
    _app("whatsapp", "WhatsApp", "whatsapp.exe", "whatsapp.root.exe"),
    _app("spotify", "Spotify", "spotify.exe"),
    _app("slack", "Slack", "slack.exe"),
    _app("zoom", "Zoom", "zoom.exe"),
    _app("discord", "Discord", "discord.exe"),
)}

# Jo user bolta hai -> APPS key. "visual studio" aur "visual studio code"
# alag apps hain, isliye dono ki apni entry - fuzzy par nahi chhoda.
ALIASES: dict[str, str] = {
    "chrome": "chrome", "google chrome": "chrome",
    "edge": "edge", "microsoft edge": "edge",
    "firefox": "firefox", "mozilla firefox": "firefox",
    "brave": "brave", "opera": "opera",
    "browser": "browser", "browsers": "browser", "web browser": "browser",
    "excel": "excel", "microsoft excel": "excel", "ms excel": "excel",
    "word": "word", "microsoft word": "word", "ms word": "word",
    "powerpoint": "powerpoint", "power point": "powerpoint", "ppt": "powerpoint",
    "outlook": "outlook", "microsoft outlook": "outlook",
    "onenote": "onenote", "one note": "onenote",
    "teams": "teams", "microsoft teams": "teams", "ms teams": "teams",
    "vscode": "vscode", "vs code": "vscode", "visual studio code": "vscode",
    "code": "vscode",
    "visual studio": "visualstudio", "microsoft visual studio": "visualstudio",
    "pdf": "pdf", "pdfs": "pdf", "acrobat": "pdf", "adobe reader": "pdf",
    "pdf reader": "pdf", "adobe acrobat": "pdf",
    "explorer": "explorer", "file explorer": "explorer",
    "files": "explorer", "folder": "explorer",
    "notepad": "notepad",
    "terminal": "terminal", "windows terminal": "terminal",
    "whatsapp": "whatsapp", "whats app": "whatsapp",
    "spotify": "spotify", "slack": "slack", "zoom": "zoom",
    "discord": "discord",
}

# Label jab process se pehchaan ho par query alias na ho
_PROCESS_LABEL = {p: a.label for a in APPS.values() if a.key != "browser"
                  for p in a.processes}

# Query me yeh shabd matlab nahi badalte - "close the excel window"
_FILLER = {
    "the", "a", "an", "my", "this", "that", "window", "windows", "app",
    "apps", "application", "applications", "program", "please", "wala",
    "wali", "wale",
}


def _normalize(text: str) -> str:
    words = re.sub(r"[^\w\s.]", " ", (text or "").lower()).split()
    return " ".join(w for w in words if w not in _FILLER)


def resolve_app(name: str) -> App | None:
    """Bola hua naam -> App. Seedha alias, "microsoft" hata kar, phir typo."""
    query = _normalize(name)
    if not query:
        return None
    if query in ALIASES:
        return APPS[ALIASES[query]]
    for prefix in ("microsoft ", "ms "):
        if query.startswith(prefix) and query[len(prefix):] in ALIASES:
            return APPS[ALIASES[query[len(prefix):]]]
    # "outlok", "visul studio" - 0.85 itna ooncha ki "visual studio" kabhi
    # "visual studio code" na ban jaye (unka ratio 0.84 hai)
    close = difflib.get_close_matches(query, ALIASES, n=1, cutoff=0.85)
    return APPS[ALIASES[close[0]]] if close else None


# --- Windows ----------------------------------------------------------------

@dataclass(frozen=True)
class WindowInfo:
    hwnd: int
    title: str
    process: str          # lowercase exe name
    pid: int
    class_name: str = ""
    minimized: bool = False
    maximized: bool = False
    foreground: bool = False

    @property
    def label(self) -> str:
        return _PROCESS_LABEL.get(self.process) or (
            self.process.removesuffix(".exe").capitalize() or self.title
        )

    def as_dict(self) -> dict:
        return {
            "title": self.title,
            "process": self.process,
            "hwnd": self.hwnd,
            "minimized": self.minimized,
            "foreground": self.foreground,
        }


def _is_cloaked(hwnd: int) -> bool:
    """UWP apps aur doosre virtual desktops ki windows "visible" hoti hain par
    dikhti nahi - Alt+Tab bhi unhe chhod deta hai."""
    value = wintypes.DWORD()
    hr = _dwmapi.DwmGetWindowAttribute(
        hwnd, DWMWA_CLOAKED, ctypes.byref(value), ctypes.sizeof(value)
    )
    return hr == 0 and value.value != 0


def _is_user_window(hwnd: int) -> bool:
    """Woh window jo Alt+Tab me dikhti - baaki sab app ke andar ke helpers."""
    if not win32gui.IsWindowVisible(hwnd):
        return False
    if not win32gui.GetWindowText(hwnd).strip():
        return False
    if win32gui.GetWindow(hwnd, win32con.GW_OWNER):
        return False   # dialog/popup - apni main window ke saath jata hai
    if win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE) & win32con.WS_EX_TOOLWINDOW:
        return False
    if win32gui.GetClassName(hwnd) in SHELL_CLASSES:
        return False
    return not _is_cloaked(hwnd)


def _process_name(pid: int) -> str:
    try:
        return psutil.Process(pid).name().lower()
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return ""


def _owner_process(hwnd: int) -> tuple[int, str]:
    _, pid = win32process.GetWindowThreadProcessId(hwnd)
    name = _process_name(pid)
    if name != "applicationframehost.exe":
        return pid, name

    # UWP apps (Settings, Calculator...) ka frame ek host process ka hota hai;
    # asli app uski child window me baithi hoti hai
    found = []

    def visit(child, _):
        _, child_pid = win32process.GetWindowThreadProcessId(child)
        if child_pid != pid:
            found.append(child_pid)
            return False
        return True

    try:
        win32gui.EnumChildWindows(hwnd, visit, None)
    except pywintypes.error:
        pass   # visit ka False enumeration rok deta hai - yeh error nahi
    if found:
        return found[0], _process_name(found[0])
    return pid, name


def _enum_windows() -> list[WindowInfo]:
    """Sab user windows, Z-order me - sabse upar (haal me dekhi) pehle."""
    if not IS_WINDOWS:
        return []

    foreground = win32gui.GetForegroundWindow()
    windows = []
    # pygetwindow EnumWindows ka kram rakhta hai = Z-order
    for win in pygetwindow.getAllWindows():
        hwnd = win._hWnd
        try:
            if not _is_user_window(hwnd):
                continue
            pid, process = _owner_process(hwnd)
            placement = win32gui.GetWindowPlacement(hwnd)[1]
            windows.append(WindowInfo(
                hwnd=hwnd,
                title=win32gui.GetWindowText(hwnd).strip(),
                process=process,
                pid=pid,
                class_name=win32gui.GetClassName(hwnd),
                minimized=placement == win32con.SW_SHOWMINIMIZED,
                maximized=placement == win32con.SW_SHOWMAXIMIZED,
                foreground=hwnd == foreground,
            ))
        except pywintypes.error:
            continue   # beech me band ho gayi - chhodo
    return windows


def list_open_windows() -> list[dict]:
    """Har khuli window: title, process name, window handle (hwnd)."""
    return [w.as_dict() for w in _enum_windows()]


def summarize_windows(windows: list[dict]) -> str:
    if not windows:
        return "No windows are open."
    names = []
    for w in windows:
        label = _PROCESS_LABEL.get(w["process"]) or w["process"].removesuffix(".exe")
        names.append(f"{label} ({w['title'][:40]})")
    return f"{_count(len(windows))} windows are open: " + "; ".join(names) + "."


# --- Matching ---------------------------------------------------------------

def _score(query: str, window: WindowInfo) -> float:
    """Kitna milta hai - 1.0 poora, 0 bilkul nahi."""
    title = window.title.lower()
    if query in title:
        return 1.0
    words = re.findall(r"[a-z0-9]+", title)
    words += re.findall(r"[a-z0-9]+", window.process.removesuffix(".exe"))
    if not words:
        return 0.0
    # Har bola hua shabd title ke sabse milte shabd se - "visul" ~ "visual"
    per_word = [
        max(difflib.SequenceMatcher(None, q, w).ratio() for w in words)
        for q in query.split()
    ]
    word_score = sum(per_word) / len(per_word)
    whole = difflib.SequenceMatcher(None, query, title).ratio()
    return max(word_score, whole)


def find_windows(
    name: str, windows: list[WindowInfo]
) -> tuple[App | None, list[WindowInfo]]:
    """Naam se windows. App pehchani gayi toh process se, warna title se."""
    app = resolve_app(name)
    if app:
        pattern = re.compile(app.title_pattern, re.I) if app.title_pattern else None
        matches = [
            w for w in windows
            if w.process in app.processes
            # PDF ka title-match browser par nahi - woh ek tab hai, poori
            # browser window band karna galat hoga
            or (pattern and pattern.search(w.title)
                and w.process not in BROWSER_PROCESSES)
        ]
        return app, matches

    query = _normalize(name)
    if not query:
        return None, []
    scored = [(_score(query, w), w) for w in windows]
    best = max((s for s, _ in scored), default=0.0)
    if best < MATCH_THRESHOLD:
        return None, []
    # Barabar ke achhe saare - "close budget" do Budget files par dono de
    cutoff = max(MATCH_THRESHOLD, best - 0.1)
    return None, [w for s, w in scored if s >= cutoff]


def _label(app: App | None, matches: list[WindowInfo], name: str) -> str:
    if app:
        return app.label
    labels = {w.label for w in matches}
    if len(labels) == 1:
        return labels.pop()
    return name.strip() or "that"


# --- Protection -------------------------------------------------------------

@dataclass
class _Context:
    jarvis_pids: set[int]

    @classmethod
    def current(cls) -> "_Context":
        """Jarvis ka process aur uske saare purvaj - terminal, VS Code,
        jo bhi ise chala raha hai. Unhe band kiya toh Jarvis bhi marega."""
        pids = {os.getpid()}
        try:
            pids |= {p.pid for p in psutil.Process().parents()}
        except psutil.Error:
            pass
        return cls(pids)


def _has_hud_tab(window: WindowInfo) -> bool:
    """HUD kisi browser window ke peeche wale tab me bhi ho sakta hai - title
    me tab dikhta nahi. Tabs padhne me ~1s lagta hai, isliye sirf browser
    band karte waqt."""
    if window.process not in BROWSER_PROCESSES:
        return False
    try:
        import browser_tools
        return any(HUD_MARKER in t["title"]
                   for t in browser_tools.list_tabs({"hwnd": window.hwnd}))
    except Exception:
        return False


def _protected_reason(window: WindowInfo, ctx: _Context,
                      check_tabs: bool = False) -> str | None:
    if window.class_name in SHELL_CLASSES:
        return "it is part of Windows itself"
    if window.pid in ctx.jarvis_pids:
        return "Jarvis is running in it"
    if HUD_MARKER in window.title or (check_tabs and _has_hud_tab(window)):
        return "it has Jarvis's own screen in it"
    return None


# --- Unsaved changes ----------------------------------------------------------

# VS Code "● file", Notepad "*file", Sublime "• file", ya aakhir me " *"
_DIRTY_TITLE = re.compile(
    r"^\s*[*●•]|[*●•]\s*(?:[-—|]|$)|\bunsaved\b|\bnot saved\b", re.I
)

_OFFICE = (
    ("Excel.Application", "excel.exe", "Workbooks"),
    ("Word.Application", "winword.exe", "Documents"),
    ("PowerPoint.Application", "powerpnt.exe", "Presentations"),
)


def _office_unsaved(processes: set[str]) -> dict[str, list[str]]:
    """Office khud batata hai kaunsi file save nahi hui (Workbook.Saved).

    Office title me unsaved ka koi nishaan nahi deta, isliye COM. Office
    busy ho (cell edit mode) toh COM mana kar deta hai - tab chupchaap
    chhod dete hain; band karte waqt Office ka apna "Save?" phir bhi aayega.
    """
    wanted = [o for o in _OFFICE if o[1] in processes]
    check_outlook = bool(processes & {"outlook.exe"})
    if not wanted and not check_outlook:
        return {}

    unsaved: dict[str, list[str]] = {}
    pythoncom.CoInitialize()   # asyncio.to_thread ke thread me COM chahiye
    try:
        for prog_id, process, collection in wanted:
            try:
                app = win32com.client.GetActiveObject(prog_id)
                names = [d.Name for d in getattr(app, collection) if not d.Saved]
            except pywintypes.com_error:
                continue
            if names:
                unsaved[process] = names

        if check_outlook:
            # Outlook me unsaved = adhoori email jo compose window me khuli hai
            try:
                outlook = win32com.client.GetActiveObject("Outlook.Application")
                for inspector in outlook.Inspectors:
                    try:
                        if not inspector.CurrentItem.Saved:
                            unsaved.setdefault("outlook.exe", []).append(
                                inspector.Caption)
                    except pywintypes.com_error:
                        pass
            except pywintypes.com_error:
                pass
    finally:
        pythoncom.CoUninitialize()
    return unsaved


def _is_unsaved(window: WindowInfo, office: dict[str, list[str]]) -> bool:
    if _DIRTY_TITLE.search(window.title):
        return True
    title = window.title.lower()
    return any(name.lower() in title for name in office.get(window.process, []))


# --- Closing ------------------------------------------------------------------

_NUMBER_WORDS = ["No", "One", "Two", "Three", "Four", "Five", "Six", "Seven",
                 "Eight", "Nine", "Ten"]


def _count(n: int) -> str:
    return _NUMBER_WORDS[n] if n < len(_NUMBER_WORDS) else str(n)


def _plural(n: int, word: str) -> str:
    return word if n == 1 else word + "s"


def _token(action: str, windows: list[WindowInfo]) -> str:
    """Band hone wali windows ki pehchaan - dekhein module docstring."""
    ids = ",".join(str(h) for h in sorted(w.hwnd for w in windows))
    return hashlib.sha1(f"{action}|{ids}".encode()).hexdigest()[:8]


def _titles(windows: list[WindowInfo], limit: int = 4) -> str:
    shown = ", ".join(f"'{w.title[:50]}'" for w in windows[:limit])
    extra = len(windows) - limit
    return shown + (f" and {extra} more" if extra > 0 else "")


def _wait_closed(windows: list[WindowInfo]) -> list[WindowInfo]:
    """Jo abhi bhi khuli hain. Tray me chhupi (Teams, Slack) band maani."""
    deadline = time.monotonic() + CLOSE_WAIT_SECONDS
    remaining = list(windows)
    while remaining and time.monotonic() < deadline:
        time.sleep(0.25)
        remaining = [
            w for w in remaining
            if win32gui.IsWindow(w.hwnd) and win32gui.IsWindowVisible(w.hwnd)
        ]
    return remaining


def _close_windows(
    targets: list[WindowInfo],
    label: str,
    action: str,
    confirm_token: str | None,
    *,
    confirm_multiple: bool,
    always_confirm_message: str | None = None,
) -> dict:
    """Asli band karna - confirmation, WM_CLOSE, aur ginti."""
    office = _office_unsaved({w.process for w in targets})
    unsaved = [w for w in targets if _is_unsaved(w, office)]
    token = _token(action, targets)

    question = always_confirm_message
    if question is None and unsaved:
        question = (
            f"{label} has unsaved changes in {_titles(unsaved)}. Close anyway? "
            f"{label} may still ask you whether to save."
        )
    if question is None and confirm_multiple and len(targets) > 1:
        question = (
            f"{_count(len(targets))} {label} windows match: {_titles(targets)}. "
            f"Close all {len(targets)}?"
        )

    if question and confirm_token != token:
        return {
            "ok": False,
            "needs_confirmation": True,
            "confirm_token": token,
            "closed_count": 0,
            "windows": [w.title for w in targets],
            "unsaved": [w.title for w in unsaved],
            "message": question,
        }

    for w in targets:
        try:
            # WM_CLOSE = X button. App ko "Save changes?" poochhne ka mauka.
            win32gui.PostMessage(w.hwnd, win32con.WM_CLOSE, 0, 0)
        except pywintypes.error:
            pass   # beech me khud band ho gayi

    still_open = _wait_closed(targets)
    closed = len(targets) - len(still_open)

    if closed == 1 and not still_open:
        message = f"{label} closed successfully."
    elif closed:
        message = f"{_count(closed)} {label} {_plural(closed, 'window')} were closed."
    else:
        message = f"{label} did not close."
    if closed == 1 and len(targets) > 1:
        message = f"One {label} window was closed."
    if still_open:
        message += (
            f" {_count(len(still_open))} {_plural(len(still_open), 'window')} "
            f"stayed open ({_titles(still_open)}) - probably asking whether to "
            "save changes. Please check."
        )

    return {
        "ok": closed > 0 and not still_open,
        "closed_count": closed,
        "closed": [w.title for w in targets if w not in still_open],
        "still_open": [w.title for w in still_open],
        "message": message,
    }


def _split_protected(
    windows: list[WindowInfo], ctx: _Context, check_tabs: bool
) -> tuple[list[WindowInfo], list[tuple[WindowInfo, str]]]:
    allowed, kept = [], []
    for w in windows:
        reason = _protected_reason(w, ctx, check_tabs)
        (kept.append((w, reason)) if reason else allowed.append(w))
    return allowed, kept


def _kept_note(kept: list[tuple[WindowInfo, str]]) -> str:
    return " ".join(f"Kept '{w.title[:50]}' open: {reason}." for w, reason in kept)


def _with_note(result: dict, note: str) -> dict:
    if note:
        result["message"] = f"{result['message']} {note}".strip()
    return result


_CURRENT_TAB = re.compile(
    r"^(?:(?:current|this|active)\s+)?(?:browser\s+)?tab$|^current browser tab$"
)


def close_window(window_name: str, confirm_token: str | None = None) -> dict:
    """Naam se window band - "Excel", "Outlook", "visual studio", "PDF".

    Kai windows mile ya unsaved kaam dikhe toh pehle poochhta hai.
    "current browser tab" browser_tools ko jata hai - poori window nahi.
    """
    if not IS_WINDOWS:
        return {"ok": False, "message": "Window control only works on Windows."}

    query = (window_name or "").lower().strip()
    if _CURRENT_TAB.match(re.sub(r"\s+", " ", query.replace("the ", ""))):
        import browser_tools
        return browser_tools.close_current_tab()

    windows = _enum_windows()
    app, matches = find_windows(window_name, windows)
    label = _label(app, matches, window_name)

    if not matches:
        if app and app.key == "pdf":
            in_browser = [w for w in windows if w.process in BROWSER_PROCESSES
                          and re.search(r"\.pdf\b", w.title, re.I)]
            if in_browser:
                return {"ok": False, "closed_count": 0, "message": (
                    f"No PDF reader window found. The PDF is open in a browser "
                    f"tab ('{in_browser[0].title[:50]}') - ask me to close the "
                    "current browser tab instead."
                )}
        return {"ok": False, "closed_count": 0,
                "message": f"No {label} window found."}

    ctx = _Context.current()
    targets, kept = _split_protected(matches, ctx, check_tabs=True)
    if not targets:
        return {"ok": False, "closed_count": 0,
                "message": f"I won't close that. {_kept_note(kept)}"}

    result = _close_windows(targets, label, f"close:{label}", confirm_token,
                            confirm_multiple=True)
    return _with_note(result, _kept_note(kept))


def close_all_windows(app_name: str, confirm_token: str | None = None) -> dict:
    """Ek app ki saari windows - "close all Chrome windows".

    "Saari" user ne khud kaha, isliye kai windows par dobara nahi poochhta;
    unsaved kaam dikhe toh zaroor.
    """
    if not IS_WINDOWS:
        return {"ok": False, "message": "Window control only works on Windows."}

    windows = _enum_windows()
    app, matches = find_windows(app_name, windows)
    label = _label(app, matches, app_name)
    if not matches:
        return {"ok": False, "closed_count": 0,
                "message": f"No {label} window found."}

    ctx = _Context.current()
    targets, kept = _split_protected(matches, ctx, check_tabs=True)
    if not targets:
        return {"ok": False, "closed_count": 0,
                "message": f"Nothing closed. {_kept_note(kept)}"}

    result = _close_windows(targets, label, f"close-all:{label}", confirm_token,
                            confirm_multiple=False)
    return _with_note(result, _kept_note(kept))


def close_all_browser_windows(confirm_token: str | None = None) -> dict:
    return close_all_windows("browser", confirm_token)


def close_all_outlook_windows(confirm_token: str | None = None) -> dict:
    return close_all_windows("outlook", confirm_token)


def close_all_excel_windows(confirm_token: str | None = None) -> dict:
    return close_all_windows("excel", confirm_token)


def _current_window(windows: list[WindowInfo], ctx: _Context) -> WindowInfo | None:
    """User ki "current" window - Jarvis ki apni screen/terminal nahi."""
    for w in windows:   # foreground pehle dekho, phir Z-order me
        if w.foreground and not _protected_reason(w, ctx):
            return w
    for w in windows:
        if not w.minimized and not _protected_reason(w, ctx):
            return w
    return None


def close_all_windows_except_current(confirm_token: str | None = None) -> dict:
    """Current window chhod kar sab band. HAMESHA pehle poochhta hai -
    aur batata hai ki kaunsi bachegi, taaki galat andaaza pakda jaye."""
    if not IS_WINDOWS:
        return {"ok": False, "message": "Window control only works on Windows."}

    windows = _enum_windows()
    ctx = _Context.current()
    current = _current_window(windows, ctx)
    if current is None:
        return {"ok": False, "closed_count": 0,
                "message": "I couldn't tell which window is current, so I closed nothing."}

    others = [w for w in windows if w.hwnd != current.hwnd]
    targets, kept = _split_protected(others, ctx, check_tabs=True)
    if not targets:
        return {"ok": False, "closed_count": 0,
                "message": f"There's nothing else to close besides '{current.title[:50]}'."}

    question = (
        f"I'll keep '{current.title[:50]}' open and close "
        f"{_count(len(targets)).lower()} other {_plural(len(targets), 'window')}: "
        f"{_titles(targets, limit=6)}."
    )
    if kept:
        question += " Jarvis's own windows stay open too."
    question += " Go ahead?"
    result = _close_windows(targets, "windows", "close-except-current",
                            confirm_token, confirm_multiple=False,
                            always_confirm_message=question)
    if not result.get("needs_confirmation"):
        closed = result["closed_count"]
        result["message"] = (
            f"{_count(closed)} {_plural(closed, 'window')} "
            f"{'was' if closed == 1 else 'were'} closed; kept "
            f"'{current.title[:50]}'."
        )
        if result["still_open"]:
            result["message"] += (
                f" {_count(len(result['still_open']))} stayed open - probably "
                "asking whether to save changes."
            )
    return _with_note(result, _kept_note(kept) if not result.get(
        "needs_confirmation") else "")


# --- Focus / minimize / maximize --------------------------------------------

def _bring_to_front(hwnd: int) -> bool:
    """Window ko aage laao, Windows ke foreground lock ke bawajood.

    Background process ka SetForegroundWindow aam taur par chupchaap fail
    hota hai. AttachThreadInput se hum abhi-wali foreground window ke thread
    ka "input" ban jate hain; tab bhi na maane toh ek Alt - Windows maanta
    hai user ne kuch dabaya, aur lock khul jata hai.
    """
    if win32gui.IsIconic(hwnd):
        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)

    foreground = win32gui.GetForegroundWindow()
    if foreground == hwnd:
        return True

    our_thread = win32api.GetCurrentThreadId()
    their_thread = (win32process.GetWindowThreadProcessId(foreground)[0]
                    if foreground else 0)
    attached = False
    try:
        if their_thread and their_thread != our_thread:
            win32process.AttachThreadInput(our_thread, their_thread, True)
            attached = True
        win32gui.BringWindowToTop(hwnd)
        win32gui.SetForegroundWindow(hwnd)
    except pywintypes.error:
        pass
    finally:
        if attached:
            try:
                win32process.AttachThreadInput(our_thread, their_thread, False)
            except pywintypes.error:
                pass

    if win32gui.GetForegroundWindow() != hwnd:
        try:
            pyautogui.press("alt")
        except pyautogui.FailSafeException:
            pass   # mouse screen ke kone me - pyautogui ka safety switch
        try:
            win32gui.SetForegroundWindow(hwnd)
        except pywintypes.error:
            pass

    time.sleep(0.1)
    return win32gui.GetForegroundWindow() == hwnd


def _single_target(window_name: str) -> tuple[str, list[WindowInfo], dict | None]:
    windows = _enum_windows()
    app, matches = find_windows(window_name, windows)
    label = _label(app, matches, window_name)
    if not matches:
        return label, [], {"ok": False, "message": f"No {label} window found."}
    return label, matches, None


def focus_window(window_name: str) -> dict:
    """ "Switch to Outlook" - sabse haal me dekhi hui matching window aage."""
    if not IS_WINDOWS:
        return {"ok": False, "message": "Window control only works on Windows."}

    label, matches, error = _single_target(window_name)
    if error:
        return error

    target = matches[0]   # Z-order me sabse upar = sabse haal me dekhi
    if not _bring_to_front(target.hwnd):
        return {"ok": False, "message": (
            f"Windows didn't let me bring {label} to the front. Click it in "
            "the taskbar - it should be flashing there."
        )}

    message = f"Switched to {label}."
    if len(matches) > 1:
        message = (f"Switched to {label} ('{target.title[:50]}') - "
                   f"{_count(len(matches) - 1).lower()} more "
                   f"{_plural(len(matches) - 1, 'window')} match.")
    return {"ok": True, "hwnd": target.hwnd, "message": message}


def minimize_window(window_name: str) -> dict:
    """Saari matching windows minimize - wapas laana aasaan hai, isliye
    kai windows par poochhna bekaar."""
    if not IS_WINDOWS:
        return {"ok": False, "message": "Window control only works on Windows."}

    label, matches, error = _single_target(window_name)
    if error:
        return error

    done = 0
    for w in matches:
        try:
            pygetwindow.Win32Window(w.hwnd).minimize()
            done += 1
        except Exception:
            pass

    if done == 1:
        message = f"{label} minimized."
    else:
        message = f"{_count(done)} {label} windows minimized."
    return {"ok": done > 0, "count": done, "message": message}


def maximize_window(window_name: str) -> dict:
    """Sabse haal me dekhi hui matching window maximize aur aage."""
    if not IS_WINDOWS:
        return {"ok": False, "message": "Window control only works on Windows."}

    label, matches, error = _single_target(window_name)
    if error:
        return error

    target = matches[0]
    try:
        pygetwindow.Win32Window(target.hwnd).maximize()
    except Exception:
        return {"ok": False, "message": f"Couldn't maximize {label}."}
    _bring_to_front(target.hwnd)

    message = f"{label} maximized."
    if len(matches) > 1:
        message = (f"Maximized the most recent of {_count(len(matches)).lower()} "
                   f"{label} windows.")
    return {"ok": True, "hwnd": target.hwnd, "message": message}


# --- Text commands ------------------------------------------------------------

_COMMANDS = [
    (re.compile(r"^(?:list|show)(?: me)?(?: all)?(?: the)?(?: open| running)?"
                r"(?: windows?| apps?)?$"), "list"),
    (re.compile(r"^close (?:all|every(?:thing)?)(?: (?:the )?(?:other )?"
                r"(?:windows?|apps?))? (?:except|but) (?:the )?(?:current|this)"
                r"(?: one| window)?$"), "close_except"),
    (re.compile(r"^close (?:all|every) (?:the )?(?P<target>.+?)(?: windows?)?$"),
     "close_all"),
    (re.compile(r"^(?:close|quit|exit) (?P<target>.+)$"), "close"),
    (re.compile(r"^bring (?P<target>.+?) to (?:the )?front$"), "focus"),
    (re.compile(r"^(?:switch|go|change) (?:over )?to (?P<target>.+)$"), "focus"),
    (re.compile(r"^(?:focus(?: on)?|activate) (?P<target>.+)$"), "focus"),
    (re.compile(r"^(?:minimi[sz]e|hide) (?P<target>.+)$"), "minimize"),
    (re.compile(r"^maximi[sz]e (?P<target>.+)$"), "maximize"),
]


def parse_command(text: str) -> tuple[str, str] | None:
    """ "Jarvis, close Chrome please" -> ("close", "chrome"). Samjha nahi toh None."""
    cleaned = re.sub(r"[^\w\s]", " ", (text or "").lower())
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    cleaned = re.sub(r"^(?:hey |ok |okay )?jarvis ", "", cleaned)
    cleaned = re.sub(r"^(?:please |can you |could you )+", "", cleaned)
    cleaned = re.sub(r" please$", "", cleaned)
    for pattern, action in _COMMANDS:
        match = pattern.match(cleaned)
        if match:
            return action, (match.groupdict().get("target") or "").strip()
    return None


def run_command(text: str, confirm_token: str | None = None) -> dict:
    """ "close chrome", "switch to vscode", "minimize teams"..."""
    parsed = parse_command(text)
    if parsed is None:
        return {"ok": False,
                "message": "Sorry, I didn't understand that window command."}

    action, target = parsed
    if action == "list":
        windows = list_open_windows()
        return {"ok": True, "windows": windows,
                "message": summarize_windows(windows)}
    if action == "close_except":
        return close_all_windows_except_current(confirm_token)
    if action == "close_all":
        return close_all_windows(target, confirm_token)
    if action == "close":
        return close_window(target, confirm_token)
    if action == "focus":
        return focus_window(target)
    if action == "minimize":
        return minimize_window(target)
    return maximize_window(target)


def main(argv: list[str]) -> int:
    """CLI: python window_manager.py "close excel" - haath se testing ke liye."""
    command = " ".join(argv) or "list windows"
    result = run_command(command)
    if result.get("needs_confirmation"):
        print(result["message"])
        if input("[y/N] ").strip().lower().startswith("y"):
            result = run_command(command, confirm_token=result["confirm_token"])
        else:
            print("Nothing closed.")
            return 1
    if "windows" in result and command.startswith(("list", "show")):
        for w in result["windows"]:
            flags = "*" if w["foreground"] else ("_" if w["minimized"] else " ")
            print(f"{flags} {w['hwnd']:>10}  {w['process']:<24} {w['title']}")
    print(result["message"])
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main(sys.argv[1:]))
