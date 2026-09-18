"""Browser me Jarvis ke haath - awaaz se chalne wale tools.

* `play_on_youtube` - gaana dhoondh kar YouTube par chala deta hai.
* `close_unused_browser_windows` - bekaar browser windows band (Windows only).
* `close_unused_browser_tabs` - kaam wali window me faltu tabs band, UI
  Automation se (browser_tabs.ps1, Windows only).

"Unused" ka matlab: kaam wali browser window (dekhein `_working_window`) aur
Jarvis ka apna HUD chhod kar baaki sab. HUD isliye, warna Jarvis apni hi
screen band kar deta.

Windows band karne ke liye WM_CLOSE bheja jata hai, `Stop-Process -Force` nahi. Yeh
wahi hai jo window ke X button par hota hai: browser apna "kya sach me band
karein?" prompt dikha sakta hai aur session theek se save karta hai. Phir bhi
poori window jati hai, matlab uske saare tabs - jo form ya draft save nahi
hua, woh chala jayega.
"""

import ctypes
import json
import re
import subprocess
import sys
import urllib.error
import urllib.request
import webbrowser
from ctypes import wintypes
from pathlib import Path
from urllib.parse import urlencode

# Jinhe browser maana jaye
BROWSER_EXES = {
    "chrome.exe",
    "msedge.exe",
    "firefox.exe",
    "brave.exe",
    "opera.exe",
    "vivaldi.exe",
}

# HUD page ka <title> isi se shuru hota hai - is window ko haath nahi lagana
HUD_MARKER = "J.A.R.V.I.S."

WM_CLOSE = 0x0010
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

IS_WINDOWS = sys.platform == "win32"

if IS_WINDOWS:
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    _EnumWindowsProc = ctypes.WINFUNCTYPE(
        wintypes.BOOL, wintypes.HWND, wintypes.LPARAM
    )

    user32.EnumWindows.argtypes = [_EnumWindowsProc, wintypes.LPARAM]
    user32.EnumWindows.restype = wintypes.BOOL
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.IsIconic.argtypes = [wintypes.HWND]
    user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.GetWindowThreadProcessId.argtypes = [
        wintypes.HWND, ctypes.POINTER(wintypes.DWORD)
    ]
    user32.PostMessageW.argtypes = [
        wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM
    ]

    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD),
    ]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]


def _window_title(hwnd) -> str:
    length = user32.GetWindowTextLengthW(hwnd)
    if length <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buf, length + 1)
    return buf.value


def _process_name(hwnd) -> str:
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    if not pid.value:
        return ""

    handle = kernel32.OpenProcess(
        PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value
    )
    if not handle:
        return ""

    try:
        size = wintypes.DWORD(4096)
        buf = ctypes.create_unicode_buffer(size.value)
        if not kernel32.QueryFullProcessImageNameW(
            handle, 0, buf, ctypes.byref(size)
        ):
            return ""
        return Path(buf.value).name.lower()
    finally:
        kernel32.CloseHandle(handle)


def list_browser_windows() -> list[dict]:
    """Har dikhne wali browser window, uske title aur process ke saath."""
    if not IS_WINDOWS:
        return []

    found: list[dict] = []
    foreground = user32.GetForegroundWindow()

    def visit(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True

        title = _window_title(hwnd)
        if not title:
            return True   # Bina title ke windows browser ke chhupe helpers hain

        process = _process_name(hwnd)
        if process in BROWSER_EXES:
            found.append({
                "hwnd": hwnd,
                "title": title,
                "process": process,
                "foreground": hwnd == foreground,
                "minimized": bool(user32.IsIconic(hwnd)),
            })
        return True

    user32.EnumWindows(_EnumWindowsProc(visit), 0)
    return found


def _working_window(windows: list[dict]) -> tuple[dict, str]:
    """Woh browser window jisme aap kaam kar rahe hain.

    "Saamne wali window" sunne me theek lagta hai, lekin awaaz se hukum dete
    waqt saamne browser hota hi nahi - HUD ya editor hota hai. Isliye: saamne
    browser hai toh wahi, warna woh jo sabse haal me istemaal hui
    (EnumWindows Z-order me sabse upar). Minimized wali kabhi nahi - usme koi
    kaam nahi kar raha.
    """
    foreground = next((w for w in windows if w["foreground"]), None)
    if foreground:
        return foreground, "abhi saamne hai"
    visible = [w for w in windows if not w["minimized"]] or windows
    return visible[0], "sabse haal me istemaal hui browser window"


def close_unused_browser_windows() -> dict:
    """Kaam wali browser window bachao, baaki band kar do."""
    if not IS_WINDOWS:
        return {"error": "Yeh sirf Windows par chalta hai.", "closed": []}

    windows = list_browser_windows()
    if not windows:
        return {
            "closed": [], "closed_count": 0, "kept": [],
            "message": "Koi browser window khuli hi nahi hai.",
        }

    keeper, keeper_why = _working_window(windows)

    closed, kept = [], []

    for window in windows:
        title = window["title"]

        if window["hwnd"] == keeper["hwnd"]:
            kept.append({"title": title, "why": keeper_why})
            continue

        if HUD_MARKER in title:
            kept.append({"title": title, "why": "Jarvis ka apna HUD"})
            continue

        # WM_CLOSE - wahi jo X button dabane par hota hai
        user32.PostMessageW(window["hwnd"], WM_CLOSE, 0, 0)
        closed.append(title)

    # Seedhi zabaan me nateeja - model ne ek baar `closed: []` ko "sab band
    # kar diya" padh liya tha. Bolne layak sach yahin likha ho.
    if closed:
        message = f"{len(closed)} browser window band ki."
    else:
        message = "Koi window band nahi ki - sirf kaam wali window khuli thi."

    return {
        "closed": closed,
        "closed_count": len(closed),
        "kept": kept,
        "message": message,
    }


# --- Tabs ------------------------------------------------------------------

TABS_SCRIPT = Path(__file__).with_name("browser_tabs.ps1")

# PowerShell ki kaali window har baar na chamke
_CREATE_NO_WINDOW = 0x08000000


def _run_tabs_script(hwnd, mode: str, payload: dict | None = None):
    """browser_tabs.ps1 chalao aur uska JSON lautao."""
    proc = subprocess.run(
        [
            "powershell", "-NoProfile", "-NonInteractive",
            "-ExecutionPolicy", "Bypass",
            "-File", str(TABS_SCRIPT),
            "-Hwnd", str(hwnd),
            "-Mode", mode,
        ],
        input=json.dumps(payload) if payload is not None else "",
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
        creationflags=_CREATE_NO_WINDOW,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip()[:300] or f"exit {proc.returncode}")
    return json.loads(proc.stdout or "null")


def list_tabs(window: dict) -> list[dict]:
    """Ek window ke tabs: id, title, selected."""
    return _run_tabs_script(window["hwnd"], "list") or []


def _is_hud(tab: dict) -> bool:
    return HUD_MARKER in tab["title"]


def close_tabs_in_window(window: dict, keep: str | None = None) -> dict:
    """Is window me kaam wale tab(s) aur ek HUD chhod kar baaki sab band.

    `keep` diya ho toh jin tabs ke title me woh aata hai, woh bachte hain.
    Na diya ho toh jo tab saamne hai woh - lekin agar saamne Jarvis ka HUD
    hi hai (jo awaaz se baat karte waqt aksar hota hai), toh kaam wala tab
    kaunsa hai yeh andaaza lagana khatarnaak hai: HUD bachta, aapka asli kaam
    band hota. Tab kuch band nahi karte aur tabs ki list lautate hain, taaki
    Jarvis poochh sake.
    """
    tabs = list_tabs(window)
    if not tabs:
        return {
            "closed": [], "closed_count": 0,
            "message": "Is window ke tabs padh nahi paye.",
        }

    work_tabs = [t for t in tabs if not _is_hud(t)]

    query = (keep or "").strip().lower()
    if query:
        keepers = [t for t in work_tabs if query in t["title"].lower()]
        if not keepers:
            return {
                "closed": [], "closed_count": 0,
                "message": f"'{keep}' naam ka koi tab nahi mila. Kuch band nahi kiya.",
                "tabs": [t["title"] for t in work_tabs],
            }
    else:
        active = next((t for t in tabs if t["selected"]), None)
        if active is None or _is_hud(active):
            return {
                "closed": [], "closed_count": 0,
                "needs_choice": True,
                "message": (
                    "Saamne Jarvis ki screen hai, isliye pata nahi kaam wala "
                    "tab kaunsa hai. Kuch band nahi kiya - user se poochho "
                    "kaunsa tab rakhna hai."
                ),
                "tabs": [t["title"] for t in work_tabs],
            }
        keepers = [active]

    # HUD ek hi kaafi hai - saamne wala, warna pehla. Purane "Offline" HUD
    # tabs bhi bekaar hi hain, woh baaki ke saath jayenge.
    huds = [t for t in tabs if _is_hud(t)]
    hud = next((t for t in huds if t["selected"]), huds[0] if huds else None)

    keep_ids = {t["id"] for t in keepers}
    if hud:
        keep_ids.add(hud["id"])
    to_close = [t for t in tabs if t["id"] not in keep_ids]

    kept_titles = [t["title"] for t in keepers]
    if not to_close:
        return {
            "closed": [], "closed_count": 0, "kept": kept_titles,
            "message": "Band karne layak koi tab nahi tha.",
        }

    _run_tabs_script(window["hwnd"], "close", {
        "close": [t["id"] for t in to_close],
        "keep": keepers[0]["id"],
    })

    # Jo script ne kaha woh nahi - jo sach me gaya, woh gino. Pinned tab
    # band nahi hota, aur unsaved form wala tab "Leave site?" poochh kar
    # ruk jata hai.
    remaining = {t["id"] for t in list_tabs(window)}
    closed = [t["title"] for t in to_close if t["id"] not in remaining]
    still_open = [t["title"] for t in to_close if t["id"] in remaining]

    message = f"{len(closed)} tab band kiye."
    if still_open:
        message += (
            f" {len(still_open)} band nahi hue - pinned hain, ya unsaved "
            "kaam ke liye browser 'Leave site?' poochh raha hai."
        )

    return {
        "closed": closed,
        "closed_count": len(closed),
        "still_open": still_open,
        "kept": kept_titles,
        "message": message,
    }


def close_unused_browser_tabs(keep: str | None = None) -> dict:
    """Kaam wali browser window me faltu tabs band (Windows only)."""
    if not IS_WINDOWS:
        return {"error": "Yeh sirf Windows par chalta hai.", "closed": []}

    windows = list_browser_windows()
    if not windows:
        return {
            "closed": [], "closed_count": 0,
            "message": "Koi browser window khuli hi nahi hai.",
        }

    window, _ = _working_window(windows)
    try:
        return close_tabs_in_window(window, keep)
    except (RuntimeError, subprocess.TimeoutExpired, ValueError) as exc:
        # PowerShell ka kachcha error bolne layak nahi - woh `detail` me
        return {
            "closed": [], "closed_count": 0,
            "message": (
                "Browser window tak pahunch nahi paya, kuch band nahi kiya. "
                "Ek baar phir try karo."
            ),
            "detail": str(exc)[:300],
        }


# --- YouTube ---------------------------------------------------------------

YOUTUBE_SEARCH = "https://www.youtube.com/results?"
YOUTUBE_WATCH = "https://www.youtube.com/watch?v="

_YOUTUBE_HEADERS = {
    # Bina browser jaisa User-Agent ke YouTube halka page deta hai jisme
    # results hote hi nahi.
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    # Europe me bina cookie ke request consent page par chali jati hai.
    # Yeh sirf "cookie banner dikha chuke" batata hai - koi account nahi.
    "Cookie": "SOCS=CAI; CONSENT=YES+cb",
}

# Pehla asli video result. "videoRenderer" isliye ki page par ads, shorts aur
# playlists bhi "videoId" rakhte hain - unhe bajana nahi hai.
_VIDEO_RE = re.compile(r'"videoRenderer":\{"videoId":"([\w-]{11})"')
_TITLE_RE = re.compile(r'"title":\{"runs":\[\{"text":"((?:[^"\\]|\\.)*)"')


def _first_video(query: str) -> tuple[str, str | None] | None:
    """Search results page se pehla video id aur uska title."""
    url = YOUTUBE_SEARCH + urlencode({"search_query": query})
    request = urllib.request.Request(url, headers=_YOUTUBE_HEADERS)
    with urllib.request.urlopen(request, timeout=8) as response:
        html = response.read().decode("utf-8", "replace")

    match = _VIDEO_RE.search(html)
    if not match:
        return None

    title = None
    # Title usi result ke andar, video id ke thoda baad aata hai
    title_match = _TITLE_RE.search(html, match.end(), match.end() + 4000)
    if title_match:
        try:
            # JSON string hai - & jaise escapes json hi sahi kholta hai
            title = json.loads(f'"{title_match.group(1)}"')
        except ValueError:
            title = None

    return match.group(1), title


def play_on_youtube(query: str) -> dict:
    """Gaana dhoondh kar default browser me YouTube par chala dein.

    Pehla video result seedha watch page par khulta hai, jo apne aap chalta
    hai. Search na ho paye (network, ya YouTube ne page badal diya) toh
    results page khol dete hain - user khud chun lega, kuch toh khulega.
    """
    query = (query or "").strip()
    if not query:
        msg = "Kya bajana hai, yeh nahi bataya."
        return {"error": msg, "message": msg}

    search_url = YOUTUBE_SEARCH + urlencode({"search_query": query})

    try:
        found = _first_video(query)
    except (urllib.error.URLError, TimeoutError, OSError):
        found = None

    if found is None:
        webbrowser.open(search_url)
        return {
            "mode": "search",
            "query": query,
            "opened": search_url,
            "message": f"Seedha video nahi mila, '{query}' ke search results khol diye.",
        }

    video_id, title = found
    watch_url = YOUTUBE_WATCH + video_id
    webbrowser.open(watch_url)
    return {
        "mode": "video",
        "query": query,
        "title": title,
        "opened": watch_url,
        "message": f"YouTube par chala diya: {title or query}",
    }
