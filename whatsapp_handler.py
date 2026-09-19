"""WhatsApp Manager Engine - Jarvis sends and reads WhatsApp messages through
WhatsApp Web, driven by Playwright.

* `WhatsAppManager.send_message(recipient, text)` - a contact name (as saved
  on your phone) or a phone number with country code.
* `WhatsAppManager.get_unread_messages(filter_mode)` - "unread", "today" or
  "recent" (last N hours): sender, latest message snippet and time per chat.
* `WhatsAppManager.summarize_and_prompt_reply(filter_mode)` - the same, as
  one sentence Jarvis can speak, plus the structured list.

One-time setup (the QR code is scanned once, never again): tell Jarvis
"WhatsApp link karo", or run

    python whatsapp_handler.py login

Either opens a visible Chrome window with its own profile in
%LOCALAPPDATA%\\Jarvis\\whatsapp\\profile. Scan the QR code with WhatsApp on
your phone (Settings > Linked devices). The session lives in that profile,
so every later start - headless, inside Jarvis - is already logged in.

Linking by voice is the safe way: the login is saved by Jarvis itself, in
the folder Jarvis reads. A terminal opened from a packaged Windows app
(the Claude desktop app, for one) has its AppData writes redirected to
that app's private copy - see `app_paths.redirected_home` - so `login`
refuses to run there.

Design notes:

* Persistent login - `launch_persistent_context` on a dedicated profile.
  Not your everyday Chrome profile: Chrome locks a profile while it is open,
  so sharing it would break either Chrome or Jarvis.
* Headless - WhatsApp Web refuses "HeadlessChrome" user agents ("A database
  error occurred ... relink your device"), so the user agent is the normal
  Chrome one with that token removed. The login window uses the same one.
* One thread - Playwright's sync API only works on the thread that started
  it, and Jarvis calls tools from a thread pool. Every browser action runs
  on the manager's own single worker thread.
* Reading never opens a chat. Everything comes from the chat list, so
  nothing is marked as read and no blue ticks are sent.
* UTF-8 - text is NFC-normalised on the way in and out, and typed with
  `keyboard.insert_text` (one CDP insertText, no per-key events), which
  carries Devanagari, Hinglish and emoji sequences intact. Before sending,
  the compose box is read back and compared with what was meant; if the
  letters differ, nothing is sent.
* Selectors - WhatsApp Web changes its markup often. Every element has a
  list of fallbacks, ending with structural ones (roles, contenteditable,
  data-icon) that do not depend on the UI language.

CLI for trying it out without Jarvis:

    python whatsapp_handler.py status
    python whatsapp_handler.py read today
    python whatsapp_handler.py read recent --hours 3
    python whatsapp_handler.py send "Rahul" "Main 5 baje aa raha hoon 👍"
    (add --show to any command to watch the browser)
"""
from __future__ import annotations

import atexit
import datetime as dt
import hashlib
import logging
import os
import re
import sys
import threading
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote

import app_paths

log = logging.getLogger("jarvis.whatsapp")

WHATSAPP_URL = "https://web.whatsapp.com/"

FILTER_MODES = ("unread", "today", "recent")
DEFAULT_RECENT_HOURS = 3

# How long to wait for WhatsApp Web to load chats (it syncs on every start)
READY_TIMEOUT = 60
# Default for single element waits
ACTION_TIMEOUT_MS = 15_000
# A whole tool call - launch + load + work - never blocks Jarvis longer
CALL_TIMEOUT = 150
# How long a "send this?" confirmation stays valid
CONFIRM_SECONDS = 300

# Chat list: how far to scroll when collecting, and how many chats to return
MAX_SCROLLS = 10
MAX_RESULTS = 25
# Longest snippet read out loud
SPOKEN_SNIPPET_CHARS = 140
# Chats named one by one in the spoken summary
SPOKEN_CHATS = 6


# --- Selectors (first match wins; last entries are language-independent) ---

SEL_LOGGED_IN = ["#pane-side", "div[aria-label='Chat list']", "#side"]
SEL_QR = ["canvas[aria-label*='Scan']", "div[data-ref] canvas", "canvas"]
SEL_SEARCH = [
    "#side div[contenteditable='true'][role='textbox']",
    "#side input[role='textbox']",
    "#side div[contenteditable='true']",
    "#side input[type='text']",
    "div[aria-label='Search input textbox']",
]
SEL_COMPOSER = [
    "#main footer div[contenteditable='true'][role='textbox']",
    "#main footer div[contenteditable='true']",
    "div[aria-label='Type a message'][contenteditable='true']",
    "#main div[contenteditable='true'][data-tab='10']",
]
SEL_SEND = [
    "#main footer button[aria-label='Send']",
    "#main footer [data-icon='wds-ic-send-filled']",
    "#main footer [data-icon='send']",
    "#main footer button[data-tab='11']",
]
SEL_CHAT_HEADER = ["#main header span[title]", "#main header span[dir='auto']", "#main header"]
SEL_POPUP = ["div[data-animate-modal-popup='true']", "div[role='dialog']"]

# Chat-list filter chips ("All", "Unread", ...). The ids are what current
# WhatsApp Web uses; the texts cover English and a few other UI languages.
UNREAD_CHIP_IDS = ("unread-filter",)
UNREAD_CHIP_TEXTS = ("unread", "ongelezen", "ungelesen", "non lus", "no leídos", "अपठित")
ALL_CHIP_IDS = ("all-filter",)
ALL_CHIP_TEXTS = ("all", "alle", "tous", "todos", "सभी")

# Words WhatsApp shows instead of a clock time for yesterday's messages
YESTERDAY_WORDS = {"yesterday", "gisteren", "gestern", "hier", "ayer", "ieri", "कल"}
WEEKDAY_WORDS = {
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
    "maandag", "dinsdag", "woensdag", "donderdag", "vrijdag", "zaterdag", "zondag",
}

# Direction marks WhatsApp wraps text in (LRE ... PDF, isolates, LRM/RLM).
# Invisible, but they end up in what Jarvis reads out. ZWJ/ZWNJ stay.
_BIDI_CONTROLS = dict.fromkeys(
    [0x200E, 0x200F, *range(0x202A, 0x202F), *range(0x2066, 0x206A)])

_CLOCK_RE = re.compile(r"^(\d{1,2})[:.](\d{2})(?:\s*([ap])\.?\s*m\.?)?$", re.IGNORECASE)
_DATE_RE = re.compile(r"^\d{1,4}[/.\-]\d{1,2}[/.\-]\d{1,4}$")


# --- Errors -----------------------------------------------------------------

class WhatsAppError(Exception):
    """A failure with a status Jarvis can act on.

    status: not_set_up, not_logged_in, login_open, offline, timeout, not_found,
    ambiguous, invalid_number, empty_message, text_mismatch, send_failed,
    browser_error.
    """

    def __init__(self, status: str, message: str, **details):
        super().__init__(message)
        self.status = status
        self.message = message
        self.details = details

    def as_result(self) -> dict:
        return {"ok": False, "status": self.status, "message": self.message, **self.details}


# --- Text safety -------------------------------------------------------------

def clean_text(text: str | None) -> str:
    """Safe, NFC-normalised UTF-8 text.

    Keeps newlines, tabs and the zero-width joiners that emoji sequences and
    Devanagari conjuncts need; drops direction marks and other control
    characters, and replaces lone surrogates (which cannot be encoded as
    UTF-8) with "?".
    """
    if not text:
        return ""
    text = text.encode("utf-8", "replace").decode("utf-8")
    text = unicodedata.normalize("NFC", text).translate(_BIDI_CONTROLS)
    text = "".join(
        ch for ch in text
        if ch in "\n\t" or unicodedata.category(ch) != "Cc"
    )
    return text.strip()


def letters_only(text: str) -> str:
    """Letters, combining marks and digits - what must survive typing.

    Whitespace, punctuation and emoji are left out: WhatsApp turns ":)" into
    an emoji and collapses spaces, which is fine. A mangled Devanagari
    letter or matra is not.
    """
    text = unicodedata.normalize("NFC", text or "")
    return "".join(
        ch for ch in text.casefold()
        if unicodedata.category(ch)[0] in "LMN"
    )


# --- Recipients --------------------------------------------------------------

def phone_digits(recipient: str) -> str | None:
    """Digits of an international phone number, or None if it isn't one.

    "+91 98765 43210" -> "919876543210". A number starting with a single 0
    has no country code - raises, because guessing one could message a
    stranger.
    """
    raw = (recipient or "").strip()
    if not re.fullmatch(r"\+?[\d\s\-().]{8,24}", raw):
        return None
    digits = re.sub(r"\D", "", raw)
    if raw.startswith("00"):
        digits = digits[2:]
    elif digits.startswith("0") and not raw.startswith("+"):
        raise WhatsAppError(
            "invalid_number",
            f"{raw} has no country code. Say it with the country code, like +31 or +91.",
        )
    if not 8 <= len(digits) <= 15:
        raise WhatsAppError("invalid_number", f"{raw} doesn't look like a phone number.")
    return digits


def _norm_name(name: str) -> str:
    """Casefolded, Latin accents dropped ("José" -> "jose"), punctuation
    to spaces. Devanagari matras and viramas are letters here, not accents."""
    name = unicodedata.normalize("NFKD", name or "").casefold()
    kept = []
    for ch in name:
        cat = unicodedata.category(ch)
        if cat.startswith("M") and ord(ch) < 0x900:
            continue                      # Latin accent
        kept.append(ch if cat[0] in "LMN" or ch == "+" else " ")
    return unicodedata.normalize("NFC", " ".join("".join(kept).split()))


def match_contact(query: str, titles: list[str]) -> tuple[str, list[str]]:
    """Pick the chat for a spoken name from WhatsApp's search results.

    Returns (status, names): ("found", [title]), ("ambiguous", candidates)
    or ("not_found", []). Exact matches win; otherwise every word of the
    query must start a word of the title ("rahul" -> "Rahul Sharma").
    """
    q = _norm_name(query)
    if not q:
        return "not_found", []
    unique = list(dict.fromkeys(t for t in titles if t))
    exact = [t for t in unique if _norm_name(t) == q]
    if len(exact) == 1:
        return "found", exact
    if len(exact) > 1:
        return "ambiguous", exact
    q_words = q.split()
    partial = []
    for title in unique:
        words = _norm_name(title).split()
        if all(any(w.startswith(qw) for w in words) for qw in q_words):
            partial.append(title)
    if len(partial) == 1:
        return "found", partial
    if partial:
        return "ambiguous", partial
    return "not_found", []


# --- Chat list parsing (pure, unit-tested) -----------------------------------

@dataclass
class ChatRow:
    name: str
    snippet: str = ""
    time_label: str = ""
    unread: int = 0
    marked_unread: bool = False
    from_me: bool = False
    pinned: bool = False
    lines: list[str] = field(default_factory=list)

    @property
    def is_unread(self) -> bool:
        return self.unread > 0 or self.marked_unread


def looks_like_time_label(text: str) -> bool:
    t = (text or "").strip().casefold()
    return bool(
        _CLOCK_RE.match(t) or _DATE_RE.match(t)
        or t in YESTERDAY_WORDS or t in WEEKDAY_WORDS
    )


def classify_time_label(label: str, now: dt.datetime) -> tuple[str, dt.datetime | None]:
    """("today", time) / ("yesterday", None) / ("older", None) / ("unknown", None).

    The chat list shows a clock time only for today's messages, "Yesterday"
    for yesterday, a weekday for this week and a date before that.
    """
    t = (label or "").strip().casefold()
    m = _CLOCK_RE.match(t)
    if m:
        hour, minute, ampm = int(m.group(1)), int(m.group(2)), m.group(3)
        if ampm:
            hour = hour % 12 + (12 if ampm == "p" else 0)
        if hour > 23 or minute > 59:
            return "unknown", None
        return "today", now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if t in YESTERDAY_WORDS:
        return "yesterday", None
    if t in WEEKDAY_WORDS or _DATE_RE.match(t):
        return "older", None
    return "unknown", None


_STATUS_ICON = re.compile(r"(check|msg-time|status-time)", re.IGNORECASE)


def parse_row(raw: dict) -> ChatRow | None:
    """One chat-list row, as extracted by `_ROWS_JS`, into a ChatRow."""
    titles = [clean_text(t) for t in raw.get("titles") or []]
    titles = [t for t in titles if t]
    if not titles:
        return None
    name = titles[0]
    lines = [clean_text(x) for x in raw.get("lines") or []]
    lines = [x for x in lines if x]

    time_label = ""
    after_name = lines[lines.index(name) + 1:] if name in lines else lines
    for line in after_name:
        if looks_like_time_label(line):
            time_label = line
            break

    snippet = titles[1] if len(titles) > 1 else clean_text(raw.get("snippet") or "")
    if not snippet:
        # Last line that is neither the name, the time nor an unread count
        rest = [x for x in after_name if x != time_label and not x.isdigit()]
        snippet = rest[-1] if rest else ""

    unread, marked = 0, False
    for badge in raw.get("badges") or []:
        text = (badge.get("text") or "").strip()
        label = (badge.get("label") or "").casefold()
        mentions_unread = any(w in label for w in UNREAD_CHIP_TEXTS)
        if text.isdigit() and (mentions_unread or text in label):
            unread = max(unread, int(text))
        elif mentions_unread and not text:
            marked = True

    icons = raw.get("icons") or []
    return ChatRow(
        name=name,
        snippet=snippet,
        time_label=time_label,
        unread=unread,
        marked_unread=marked,
        from_me=any(_STATUS_ICON.search(i or "") for i in icons),
        pinned=any("pin" in (i or "") for i in icons),
        lines=lines,
    )


def select_rows(
    rows: list[ChatRow],
    filter_mode: str,
    now: dt.datetime,
    hours: float = DEFAULT_RECENT_HOURS,
) -> list[ChatRow]:
    """Keep the rows the filter asks for.

    unread - chats with an unread badge (or marked unread).
    today  - the latest message is from today and was not sent by you.
    recent - the same, within the last `hours`. A "Yesterday" chat counts
             when the window reaches back past midnight; the list does not
             show its exact time, so it may be slightly older.
    """
    keep = []
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    for row in rows:
        if filter_mode == "unread":
            if row.is_unread:
                keep.append(row)
            continue
        if row.from_me:
            continue
        kind, when = classify_time_label(row.time_label, now)
        if filter_mode == "today":
            if kind == "today":
                keep.append(row)
        elif filter_mode == "recent":
            window_start = now - dt.timedelta(hours=hours)
            if kind == "today" and when is not None and when >= window_start:
                keep.append(row)
            elif kind == "yesterday" and window_start < midnight:
                keep.append(row)
    return keep


def row_to_message(row: ChatRow) -> dict:
    return {
        "sender": row.name,
        "text": row.snippet,
        "time": row.time_label,
        "unread": row.unread if row.unread else (1 if row.marked_unread else 0),
    }


# --- Spoken summary ------------------------------------------------------------

def _plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


_URL_RE = re.compile(r"(https?://|www\.)\S+", re.IGNORECASE)
# WhatsApp formatting: *bold*, _italic_, ~strike~ around a word
_FORMAT_RE = re.compile(r"(?<!\w)[*_~]+|[*_~]+(?!\w)")


def _speakable(text: str, limit: int = SPOKEN_SNIPPET_CHARS) -> str:
    """Snippet as it should sound: links as "a link", no formatting marks."""
    text = _URL_RE.sub("a link", text or "")
    text = _FORMAT_RE.sub("", text)
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0]
    return cut.rstrip(",.;:") + "..."


def build_summary(
    messages: list[dict],
    filter_mode: str = "today",
    hours: float = DEFAULT_RECENT_HOURS,
) -> str:
    """A TTS-friendly summary that ends by offering to reply."""
    hours_text = f"{hours:g} {'hour' if hours == 1 else 'hours'}"
    if not messages:
        return {
            "unread": "You have no unread WhatsApp messages.",
            "today": "You have no new WhatsApp messages from today.",
            "recent": f"You have no new WhatsApp messages from the last {hours_text}.",
        }.get(filter_mode, "You have no new WhatsApp messages.")

    chats = len(messages)
    if filter_mode == "unread":
        total = sum(max(m.get("unread") or 0, 1) for m in messages)
        if total > chats:
            head = f"You have {total} unread messages in {_plural(chats, 'chat')}."
        else:
            head = f"You have {_plural(total, 'unread message')}."
    elif filter_mode == "recent":
        head = f"You have {_plural(chats, 'new message')} from the last {hours_text}."
    else:
        head = f"You have {_plural(chats, 'new message')} from today."

    parts = []
    for m in messages[:SPOKEN_CHATS]:
        text = _speakable(m.get("text", ""))
        count = m.get("unread") or 0
        if count > 1:
            parts.append(f"{m['sender']} sent {count} messages, the latest: '{text}'")
        elif text:
            parts.append(f"{m['sender']} says: '{text}'")
        else:
            parts.append(f"{m['sender']} sent a message")
    extra = chats - len(parts)
    if extra > 0:
        parts.append(f"{_plural(extra, 'more chat')}")

    if len(parts) == 1:
        body = parts[0]
    else:
        body = ", ".join(parts[:-1]) + ", and " + parts[-1]
    return f"{head} {body}. Would you like me to send a reply to any of these contacts?"


# --- In-page scripts -------------------------------------------------------------

# Text of an element with WhatsApp's emoji <img alt="😀"> put back as characters
_TEXT_OF_JS = r"""
function textOf(el) {
  if (!el) return '';
  let out = '';
  const walk = (node) => {
    if (node.nodeType === Node.TEXT_NODE) { out += node.nodeValue; return; }
    if (node.nodeType !== Node.ELEMENT_NODE) return;
    if (node.tagName === 'IMG' && node.alt) { out += node.alt; return; }
    if (node.tagName === 'BR') { out += '\n'; return; }
    const block = /^(DIV|P)$/.test(node.tagName) && out && !out.endsWith('\n');
    if (block) out += '\n';
    for (const child of node.childNodes) walk(child);
  };
  walk(el);
  return out;
}
"""

_ROWS_JS = "() => {" + _TEXT_OF_JS + r"""
  const pane = document.querySelector('#pane-side')
            || document.querySelector("[aria-label='Chat list']");
  if (!pane) return null;
  let rows = [...pane.querySelectorAll("[role='row']")];
  if (!rows.length) rows = [...pane.querySelectorAll("[role='listitem']")];
  return rows.map(row => {
    const titled = [...row.querySelectorAll('span[title]')];
    return {
      titles: titled.map(s => s.getAttribute('title') || textOf(s)),
      snippet: titled.length > 1 ? textOf(titled[1]) : '',
      lines: (row.innerText || '').split('\n').map(s => s.trim()).filter(Boolean),
      badges: [...row.querySelectorAll('span[aria-label]')].map(s => ({
        label: s.getAttribute('aria-label') || '', text: (s.innerText || '').trim()})),
      icons: [...row.querySelectorAll('[data-icon]')].map(e => e.getAttribute('data-icon')),
    };
  });
}"""

_CHIP_JS = r"""([ids, texts]) => {
  const buttons = [...document.querySelectorAll("#side button, #side [role='tab']")];
  const hit = buttons.find(b => ids.includes(b.id))
           || buttons.find(b => texts.includes(
                (b.innerText || '').trim().split('\n')[0].trim().toLowerCase()));
  if (!hit) return false;
  hit.click();
  return true;
}"""

_SCROLL_JS = r"""(top) => {
  const pane = document.querySelector('#pane-side');
  if (!pane) return false;
  const before = pane.scrollTop;
  if (top) pane.scrollTop = 0; else pane.scrollBy(0, pane.clientHeight * 0.8);
  return pane.scrollTop !== before;
}"""

# The newest message in the open chat. Its direction isn't marked reliably
# any more (no .message-out class, ids without the old "true_" prefix), so
# sending is confirmed by a NEW message id with our text instead.
_LAST_MSG_JS = "() => {" + _TEXT_OF_JS + r"""
  const main = document.querySelector('#main');
  if (!main) return null;
  const withId = [...main.querySelectorAll('[data-id]')];
  const msgs = withId.filter(e => e.querySelector('[data-pre-plain-text], .selectable-text'));
  const last = (msgs.length ? msgs : withId).pop();
  if (!last) return {id: '', text: '', icons: []};
  const body = last.querySelector('[data-pre-plain-text] .selectable-text')
            || last.querySelector('.selectable-text') || last;
  return {
    id: last.getAttribute('data-id') || '',
    text: textOf(body),
    icons: [...last.querySelectorAll('[data-icon]')].map(e => e.getAttribute('data-icon')),
  };
}"""

_ELEMENT_TEXT_JS = "(el) => {" + _TEXT_OF_JS + " return textOf(el); }"


# --- The manager -------------------------------------------------------------------

class WhatsAppManager:
    """WhatsApp Web in a persistent Chrome profile, driven from one thread.

    Public methods are thread-safe: they hand the work to the manager's own
    worker thread and wait for it. Failures come back as WhatsAppError with
    a status (see the class), never as raw Playwright exceptions.
    """

    def __init__(
        self,
        profile_dir: Path | None = None,
        headless: bool | None = None,
        channel: str | None = None,
    ):
        base = app_paths.data_dir("whatsapp")
        self.profile_dir = Path(profile_dir) if profile_dir else base / "profile"
        self._channel_file = self.profile_dir.parent / "browser.txt"
        self._linked_file = self.profile_dir.parent / "linked"
        if headless is None:
            headless = os.environ.get("JARVIS_WHATSAPP_HEADLESS", "1") != "0"
        self.headless = headless
        self._channel = channel or os.environ.get("JARVIS_WHATSAPP_CHANNEL")
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="whatsapp")
        self._headless_default = headless
        self._login_future = None
        self._pw = None
        self._ctx = None
        self._page = None

    # ---- public API ----

    def send_message(self, recipient: str, message_text: str) -> dict:
        """Send `message_text` to a contact name or an international number.

        Returns {"ok": True, "status": "sent" | "queued", "recipient", "message"}.
        "queued" means WhatsApp accepted it but it is still waiting for a
        connection (clock icon). Raises WhatsAppError otherwise.
        """
        text = clean_text(message_text)
        if not text:
            raise WhatsAppError("empty_message", "There is no message to send.")
        return self._call(self._send, recipient, text)

    def find_recipient(self, recipient: str) -> dict:
        """Resolve a spoken name to one chat without opening it.

        {"ok": True, "recipient": <chat name or +number>, "target": <what
        send_message should get>}. Raises not_found / ambiguous.
        """
        return self._call(self._find_recipient, recipient)

    def get_unread_messages(
        self,
        filter_mode: str = "unread",
        hours: float = DEFAULT_RECENT_HOURS,
    ) -> list[dict]:
        """Latest message per chat: [{"sender", "text", "time", "unread"}].

        filter_mode: "unread" (unread badge), "today" (received today) or
        "recent" (received in the last `hours`). Never opens a chat, so
        nothing is marked as read.
        """
        mode = (filter_mode or "unread").strip().lower()
        if mode not in FILTER_MODES:
            raise ValueError(f"filter_mode must be one of {FILTER_MODES}, not {filter_mode!r}")
        hours = float(hours or DEFAULT_RECENT_HOURS)
        return self._call(self._read, mode, hours)

    def summarize_and_prompt_reply(
        self,
        filter_mode: str = "today",
        hours: float = DEFAULT_RECENT_HOURS,
    ) -> dict:
        """{"ok", "message": spoken summary, "messages": [...], "count"}.

        The summary ends with an offer to reply, so Jarvis can take the
        follow-up ("reply to Rahul saying I'll be there") and call
        send_message with a sender from `messages`.
        """
        mode = (filter_mode or "today").strip().lower()
        if mode not in FILTER_MODES:
            mode = "today"
        try:
            messages = self.get_unread_messages(mode, hours)
        except WhatsAppError as exc:
            return exc.as_result()
        return {
            "ok": True,
            "status": "ok",
            "filter_mode": mode,
            "count": len(messages),
            "messages": messages,
            "message": build_summary(messages, mode, float(hours or DEFAULT_RECENT_HOURS)),
        }

    def status(self) -> dict:
        """Launch if needed and report whether WhatsApp Web is logged in."""
        return self._call(self._status)

    def login(self, timeout: float = 180) -> dict:
        """Open a visible window and wait for the QR code to be scanned."""
        return self._call(self._login, timeout, timeout=timeout + 60)

    def start_login(self, timeout: float = 180) -> dict:
        """Open the QR window and return at once - for Jarvis, which can't
        sit in a tool call for minutes. Other calls answer login_open until
        the code is scanned or `timeout` runs out."""
        if self._login_open():
            return {"ok": True, "status": "login_open",
                    "message": "The WhatsApp login window is already open. Scan the QR code in it."}
        self._login_future = self._executor.submit(self._guarded, self._login, timeout)
        return {
            "ok": True,
            "status": "login_open",
            "message": (
                "I've opened a WhatsApp window on your screen. On your phone open WhatsApp, "
                "go to Settings, Linked devices, Link a device, and scan the QR code. "
                f"You have {round(timeout / 60)} minutes. Tell me when it's done."
            ),
        }

    def _login_open(self) -> bool:
        return self._login_future is not None and not self._login_future.done()

    def warm_up(self) -> None:
        """Start the browser in the background so the first request is fast."""
        self._executor.submit(self._warm_up)

    def close(self) -> None:
        """Close the browser. Safe to call more than once."""
        try:
            self._executor.submit(self._shutdown).result(timeout=15)
        except Exception as exc:   # closing must never raise
            log.debug("WhatsApp close: %s", exc)
        self._executor.shutdown(wait=False, cancel_futures=True)

    # ---- worker-thread plumbing ----

    def _call(self, fn, *args, timeout: float = CALL_TIMEOUT):
        if self._login_open():
            raise WhatsAppError(
                "login_open", "The WhatsApp login window is still open - scan the QR code first.")
        future = self._executor.submit(self._guarded, fn, *args)
        try:
            return future.result(timeout=timeout)
        except FutureTimeout:
            raise WhatsAppError(
                "timeout", "WhatsApp took too long to respond. Try again in a moment."
            ) from None

    def _guarded(self, fn, *args):
        """Runs on the worker thread: map Playwright failures to WhatsAppError."""
        from playwright.sync_api import Error as PlaywrightError
        from playwright.sync_api import TimeoutError as PlaywrightTimeout
        try:
            return fn(*args)
        except WhatsAppError:
            raise
        except PlaywrightTimeout as exc:
            log.warning("WhatsApp timeout in %s: %s", fn.__name__, exc)
            raise WhatsAppError(
                "timeout", "WhatsApp Web didn't respond in time. Try again."
            ) from None
        except PlaywrightError as exc:
            log.warning("WhatsApp browser error in %s: %s", fn.__name__, exc)
            # Closed window, crashed renderer - start fresh next time
            self._shutdown()
            raise WhatsAppError(
                "browser_error", "The WhatsApp browser stopped working. Try again."
            ) from None

    # ---- browser lifecycle (worker thread only) ----

    def _channels(self) -> list[str | None]:
        if self._channel:
            return [self._channel]
        try:
            saved = self._channel_file.read_text(encoding="utf-8").strip()
            if saved:
                # The profile belongs to that browser - don't switch
                return [None if saved == "chromium" else saved]
        except OSError:
            pass
        return ["chrome", "msedge", None]

    def _launch(self) -> None:
        from playwright.sync_api import Error as PlaywrightError
        from playwright.sync_api import sync_playwright

        if self._pw is None:
            self._pw = sync_playwright().start()
        self.profile_dir.mkdir(parents=True, exist_ok=True)

        errors = []
        for channel in self._channels():
            try:
                agent = self._user_agent(channel)
                ctx = self._pw.chromium.launch_persistent_context(
                    str(self.profile_dir),
                    channel=channel,
                    headless=self.headless,
                    user_agent=agent,
                    locale="en-US",
                    viewport={"width": 1280, "height": 900},
                    ignore_default_args=["--enable-automation"],
                )
            except PlaywrightError as exc:
                errors.append(f"{channel or 'chromium'}: {str(exc).splitlines()[0]}")
                continue
            self._ctx = ctx
            self._page = ctx.pages[0] if ctx.pages else ctx.new_page()
            self._page.set_default_timeout(ACTION_TIMEOUT_MS)
            try:
                self._channel_file.write_text(channel or "chromium", encoding="utf-8")
            except OSError:
                pass
            log.info("WhatsApp browser started (%s, headless=%s)",
                     channel or "chromium", self.headless)
            return

        log.warning("WhatsApp browser failed to start: %s", "; ".join(errors))
        raise WhatsAppError(
            "browser_error",
            "Couldn't start the WhatsApp browser. If another Jarvis or a login "
            "window is already open, close it and try again.",
            details="; ".join(errors),
        )

    def _user_agent(self, channel: str | None) -> str:
        """The browser's own user agent without the 'Headless' marker."""
        browser = self._pw.chromium.launch(channel=channel, headless=True)
        try:
            agent = browser.new_page().evaluate("navigator.userAgent")
        finally:
            browser.close()
        return agent.replace("HeadlessChrome", "Chrome")

    def _alive(self) -> bool:
        if self._ctx is None or self._page is None or self._page.is_closed():
            return False
        try:
            self._page.evaluate("1")
            return True
        except Exception:
            return False

    def _shutdown(self) -> None:
        for close in (
            lambda: self._ctx and self._ctx.close(),
            lambda: self._pw and self._pw.stop(),
        ):
            try:
                close()
            except Exception as exc:
                log.debug("WhatsApp shutdown: %s", exc)
        self._ctx = self._page = self._pw = None

    def _warm_up(self) -> None:
        try:
            self._guarded(self._ready)
        except WhatsAppError as exc:
            log.info("WhatsApp warm-up: %s", exc.message)

    # ---- page state ----

    def _first(self, selectors: list[str], root=None):
        """First visible element for any of the selectors, or None."""
        root = root or self._page
        for sel in selectors:
            loc = root.locator(sel).first
            try:
                if loc.count() and loc.is_visible():
                    return loc
            except Exception:
                continue
        return None

    def _page_state(self) -> str:
        """ready | qr | relink | loading."""
        page = self._page
        if self._first(SEL_LOGGED_IN):
            return "ready"
        body = page.evaluate("() => (document.body && document.body.innerText) || ''")
        if "relink your device" in body.lower() or "database error" in body.lower():
            return "relink"
        if self._first(SEL_QR) and ("scan" in body.lower() or "log in" in body.lower()):
            return "qr"
        return "loading"

    def _ready(self, timeout: float = READY_TIMEOUT) -> None:
        """Browser up, WhatsApp Web loaded and logged in - or WhatsAppError."""
        if not self._alive():
            self._shutdown()
            self._launch()
        page = self._page
        if not page.url.startswith(WHATSAPP_URL):
            page.goto(WHATSAPP_URL, wait_until="domcontentloaded")

        deadline = time.monotonic() + timeout
        state = "loading"
        while time.monotonic() < deadline:
            state = self._page_state()
            if state == "ready":
                self._mark_linked(True)
                self._check_online()
                return
            if state in ("qr", "relink"):
                break
            page.wait_for_timeout(500)

        if state in ("qr", "relink"):
            self._mark_linked(False)
            raise WhatsAppError(
                "not_logged_in",
                "WhatsApp isn't linked to Jarvis yet. I can open the login window so you "
                "can scan the QR code with your phone - just say 'link WhatsApp'.",
            )
        if not page.evaluate("navigator.onLine"):
            raise WhatsAppError("offline", "The computer seems to be offline.")
        raise WhatsAppError("timeout", "WhatsApp Web is taking too long to load.")

    def _mark_linked(self, linked: bool) -> None:
        """Remember whether WhatsApp is linked - see `is_set_up`."""
        try:
            if linked:
                self._linked_file.parent.mkdir(parents=True, exist_ok=True)
                self._linked_file.touch()
            else:
                self._linked_file.unlink(missing_ok=True)
        except OSError:
            pass

    def _check_online(self) -> None:
        if not self._page.evaluate("navigator.onLine"):
            raise WhatsAppError("offline", "The computer seems to be offline, so WhatsApp can't connect.")

    # ---- status / login ----

    def _status(self) -> dict:
        try:
            self._ready()
        except WhatsAppError as exc:
            return exc.as_result()
        return {"ok": True, "status": "ready", "message": "WhatsApp is connected."}

    def _login(self, timeout: float) -> dict:
        """Visible window until the QR code is scanned, then back to headless."""
        self._shutdown()              # it may be headless - reopen visibly
        self.headless = False
        try:
            self._launch()
            page = self._page
            page.goto(WHATSAPP_URL, wait_until="domcontentloaded")
            log.info("WhatsApp login window open - waiting for the QR code to be scanned")
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                if self._page_state() == "ready":
                    self._mark_linked(True)
                    # Give the first sync a moment to write the session to disk
                    page.wait_for_timeout(8000)
                    log.info("WhatsApp linked - session saved in %s", self.profile_dir)
                    return {"ok": True, "status": "ready",
                            "message": f"WhatsApp is linked. Session saved in {self.profile_dir}."}
                page.wait_for_timeout(1000)
            raise WhatsAppError("timeout", "The QR code wasn't scanned in time. Try linking again.")
        finally:
            # Close the visible window; the next request reopens headless
            self._shutdown()
            self.headless = self._headless_default

    # ---- reading ----

    def _read(self, mode: str, hours: float) -> list[dict]:
        self._ready()
        now = dt.datetime.now()
        page = self._page
        chip = False
        try:
            if mode == "unread":
                chip = page.evaluate(_CHIP_JS, [list(UNREAD_CHIP_IDS), list(UNREAD_CHIP_TEXTS)])
            else:
                page.evaluate(_CHIP_JS, [list(ALL_CHIP_IDS), list(ALL_CHIP_TEXTS)])
            page.wait_for_timeout(700)
            rows = self._collect_rows(mode, now, hours)
        finally:
            if chip:
                page.evaluate(_CHIP_JS, [list(ALL_CHIP_IDS), list(ALL_CHIP_TEXTS)])
            page.evaluate(_SCROLL_JS, True)

        selected = select_rows(rows, mode, now, hours)
        log.info("WhatsApp %s: %d of %d chats%s", mode, len(selected), len(rows),
                 "" if mode != "unread" else
                 " (unread filter)" if chip else " (unread filter not found, scanned list)")
        return [row_to_message(r) for r in selected[:MAX_RESULTS]]

    def _collect_rows(self, mode: str, now: dt.datetime, hours: float) -> list[ChatRow]:
        """Scroll the (virtualised) chat list and gather each chat once."""
        page = self._page
        page.evaluate(_SCROLL_JS, True)
        page.wait_for_timeout(300)
        seen: dict[str, ChatRow] = {}
        window_start = now - dt.timedelta(hours=hours)
        for _ in range(MAX_SCROLLS):
            raw_rows = page.evaluate(_ROWS_JS) or []
            new = 0
            past_window = False
            for raw in raw_rows:
                row = parse_row(raw)
                if row is None or row.name in seen:
                    continue
                seen[row.name] = row
                new += 1
                if mode != "unread" and not row.pinned:
                    # Chats are sorted newest first (after pinned ones)
                    kind, when = classify_time_label(row.time_label, now)
                    if kind == "older" or (
                        mode == "recent" and kind == "today" and when and when < window_start
                    ) or (mode == "today" and kind == "yesterday"):
                        past_window = True
            if past_window or len(seen) >= MAX_RESULTS * 2:
                break
            if not page.evaluate(_SCROLL_JS, False) and not new:
                break
            page.wait_for_timeout(400)
        return list(seen.values())

    # ---- sending ----

    def _search(self, query: str) -> list[str]:
        """Type into the chat search box and return the result titles."""
        page = self._page
        box = self._first(SEL_SEARCH)
        if box is None:
            raise WhatsAppError("send_failed", "Couldn't find WhatsApp's search box.")
        box.click()
        page.keyboard.press("Control+A")
        page.keyboard.press("Backspace")
        page.keyboard.insert_text(query)

        # Results update as you type - wait until they stop changing
        titles: list[str] = []
        stable_since = time.monotonic()
        deadline = time.monotonic() + 6
        while time.monotonic() < deadline:
            page.wait_for_timeout(400)
            rows = [parse_row(r) for r in (page.evaluate(_ROWS_JS) or [])]
            current = [r.name for r in rows if r]
            if current != titles:
                titles, stable_since = current, time.monotonic()
            elif titles and time.monotonic() - stable_since > 1.0:
                break
        return titles

    def _clear_search(self) -> None:
        try:
            box = self._first(SEL_SEARCH)
            if box:
                box.click()
                self._page.keyboard.press("Control+A")
                self._page.keyboard.press("Backspace")
            self._page.keyboard.press("Escape")
        except Exception as exc:
            log.debug("clear search: %s", exc)

    def _find_recipient(self, recipient: str) -> dict:
        recipient = clean_text(recipient)
        digits = phone_digits(recipient)
        if digits:
            return {"ok": True, "recipient": f"+{digits}", "target": f"+{digits}"}
        if not recipient:
            raise WhatsAppError("not_found", "Who should I send it to?")
        self._ready()
        try:
            status, names = match_contact(recipient, self._search(recipient))
        finally:
            self._clear_search()
        return self._resolution(recipient, status, names)

    @staticmethod
    def _resolution(recipient: str, status: str, names: list[str]) -> dict:
        if status == "found":
            return {"ok": True, "recipient": names[0], "target": names[0]}
        if status == "ambiguous":
            shown = ", ".join(names[:5])
            raise WhatsAppError(
                "ambiguous", f"More than one chat matches '{recipient}': {shown}. Which one?",
                candidates=names[:10],
            )
        raise WhatsAppError(
            "not_found", f"I couldn't find '{recipient}' in your WhatsApp chats or contacts.")

    def _open_chat_by_number(self, digits: str) -> str:
        page = self._page
        page.goto(f"{WHATSAPP_URL}send?phone={quote(digits)}", wait_until="domcontentloaded")
        deadline = time.monotonic() + READY_TIMEOUT
        while time.monotonic() < deadline:
            if self._first(SEL_COMPOSER):
                return self._chat_title() or f"+{digits}"
            popup = self._first(SEL_POPUP)
            if popup is not None:
                text = (popup.inner_text() or "").lower()
                if "invalid" in text or "not on whatsapp" in text:
                    page.keyboard.press("Escape")
                    raise WhatsAppError(
                        "invalid_number", f"+{digits} isn't on WhatsApp, or the number is wrong.")
            if self._page_state() in ("qr", "relink"):
                self._ready(timeout=1)   # raises not_logged_in
            page.wait_for_timeout(500)
        raise WhatsAppError("timeout", f"The chat with +{digits} didn't open in time.")

    def _open_chat_by_name(self, recipient: str) -> str:
        page = self._page
        titles = self._search(recipient)
        status, names = match_contact(recipient, titles)
        if status != "found":
            self._clear_search()
            self._resolution(recipient, status, names)   # raises
        name = names[0]
        spans = page.locator("#pane-side span[title]")
        all_titles = spans.evaluate_all("els => els.map(e => e.getAttribute('title'))")
        wanted = [i for i, t in enumerate(all_titles) if clean_text(t) == name]
        if not wanted:
            self._clear_search()
            raise WhatsAppError("not_found", f"'{name}' disappeared from the search results.")
        spans.nth(wanted[0]).click()
        composer = self._wait_for(SEL_COMPOSER, 10)
        if composer is None:
            raise WhatsAppError("send_failed", f"The chat with {name} didn't open.")
        header = self._chat_title()
        if header and _norm_name(header) != _norm_name(name):
            raise WhatsAppError(
                "send_failed", f"Opened '{header}' instead of '{name}', so I didn't send anything.")
        return name

    def _chat_title(self) -> str:
        el = self._first(SEL_CHAT_HEADER)
        if el is None:
            return ""
        title = el.get_attribute("title") or el.inner_text() or ""
        return clean_text(title.split("\n")[0])

    def _wait_for(self, selectors: list[str], seconds: float):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            el = self._first(selectors)
            if el is not None:
                return el
            self._page.wait_for_timeout(250)
        return None

    def _type_message(self, text: str) -> None:
        """Put `text` in the compose box and verify it arrived intact."""
        page = self._page
        composer = self._wait_for(SEL_COMPOSER, 10)
        if composer is None:
            raise WhatsAppError("send_failed", "Couldn't find the message box.")
        composer.click()
        # A draft may already be there - replace it
        page.keyboard.press("Control+A")
        page.keyboard.press("Backspace")
        for i, line in enumerate(text.split("\n")):
            if i:
                page.keyboard.press("Shift+Enter")   # Enter alone would send
            if line:
                page.keyboard.insert_text(line)
        page.wait_for_timeout(300)

        typed = composer.evaluate(_ELEMENT_TEXT_JS)
        if letters_only(typed) != letters_only(text):
            page.keyboard.press("Control+A")
            page.keyboard.press("Backspace")
            log.warning("WhatsApp compose box text differs from the message; not sent")
            raise WhatsAppError(
                "text_mismatch",
                "The message didn't come out right in WhatsApp, so I didn't send it.")

    def _click_send(self) -> None:
        button = self._wait_for(SEL_SEND, 5)
        if button is not None:
            button.click()
        else:
            # The send button's markup changed - Enter sends too
            self._page.keyboard.press("Enter")

    def _confirm_sent(self, text: str, before: dict | None) -> str:
        """sent | queued, once a new message with our text shows up - or
        send_failed."""
        page = self._page
        want = letters_only(text)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            page.wait_for_timeout(400)
            last = page.evaluate(_LAST_MSG_JS)
            if not last or not last["id"]:
                continue
            changed = before is None or last["id"] != before.get("id")
            if changed and want and letters_only(last["text"]).endswith(want[-60:]):
                icons = " ".join(i or "" for i in last["icons"])
                return "queued" if "time" in icons else "sent"
        composer = self._first(SEL_COMPOSER)
        if composer is not None and not letters_only(composer.evaluate(_ELEMENT_TEXT_JS)):
            # Box emptied, bubble not recognised - WhatsApp took it
            log.warning("WhatsApp: sent, but the new message wasn't found to confirm it")
            return "sent"
        raise WhatsAppError("send_failed", "I typed the message but WhatsApp didn't send it.")

    def _send(self, recipient: str, text: str) -> dict:
        self._ready()
        recipient = clean_text(recipient)
        digits = phone_digits(recipient)
        name = self._open_chat_by_number(digits) if digits else self._open_chat_by_name(recipient)
        before = self._page.evaluate(_LAST_MSG_JS)
        self._type_message(text)
        self._click_send()
        status = self._confirm_sent(text, before)
        self._clear_search()
        log.info("WhatsApp message to %s: %s (%d chars)", name, status, len(text))
        message = (f"Message sent to {name}." if status == "sent" else
                   f"Message to {name} is queued - WhatsApp will send it when the phone reconnects.")
        return {"ok": True, "status": status, "recipient": name, "message": message}


# --- Jarvis voice tools ---------------------------------------------------------------

_manager: WhatsAppManager | None = None
_manager_lock = threading.Lock()
# confirm_token -> (recipient as spoken, text, resolved target, label, expiry)
_pending: dict[str, tuple[str, str, str, str, float]] = {}


def manager() -> WhatsAppManager:
    global _manager
    with _manager_lock:
        if _manager is None:
            _manager = WhatsAppManager()
        return _manager


def is_set_up() -> bool:
    """True once WhatsApp Web was seen logged in (after `login`), until it
    shows the QR code again (unlinked from the phone)."""
    return (app_paths.data_dir("whatsapp") / "linked").is_file()


def warm_up() -> None:
    """Start WhatsApp Web in the background - only if it was set up."""
    if is_set_up():
        manager().warm_up()


def shutdown() -> None:
    global _manager
    with _manager_lock:
        if _manager is not None:
            _manager.close()
            _manager = None


atexit.register(shutdown)


def _send_token(target: str, text: str) -> str:
    return hashlib.sha1(f"{target}|{letters_only(text)}|{text}".encode("utf-8")).hexdigest()[:8]


def check_whatsapp_tool(args: dict) -> dict:
    """Voice tool: summary of unread / today's / recent messages."""
    mode = (args.get("filter_mode") or "today").strip().lower()
    hours = args.get("hours") or DEFAULT_RECENT_HOURS
    try:
        return manager().summarize_and_prompt_reply(mode, hours)
    except WhatsAppError as exc:
        return exc.as_result()


def send_whatsapp_tool(args: dict) -> dict:
    """Voice tool: send a message - only after the user confirmed it.

    First call: finds the chat and answers needs_confirmation with a
    confirm_token and the exact message. Called again with that token (and
    the same recipient and text), it sends.
    """
    recipient = clean_text(args.get("recipient"))
    text = clean_text(args.get("message"))
    token = (args.get("confirm_token") or "").strip()
    now = time.monotonic()
    for key in [k for k, v in _pending.items() if v[4] < now]:
        del _pending[key]

    if not text:
        return WhatsAppError("empty_message", "What should the message say?").as_result()
    if not recipient:
        return WhatsAppError("not_found", "Who should I send it to?").as_result()

    pending = _pending.get(token) if token else None
    if pending and pending[0] == recipient and pending[1] == text:
        del _pending[token]
        try:
            return manager().send_message(pending[2], text)
        except WhatsAppError as exc:
            return exc.as_result()

    try:
        found = manager().find_recipient(recipient)
    except WhatsAppError as exc:
        return exc.as_result()
    new_token = _send_token(found["target"], text)
    _pending[new_token] = (recipient, text, found["target"], found["recipient"],
                           now + CONFIRM_SECONDS)
    return {
        "ok": False,
        "needs_confirmation": True,
        "confirm_token": new_token,
        "recipient": found["recipient"],
        "text": text,
        "message": f"Send this WhatsApp to {found['recipient']}: '{text}'?",
    }


def link_whatsapp_tool(_args: dict) -> dict:
    """Voice tool: open the QR login window from inside Jarvis."""
    try:
        return manager().start_login()
    except WhatsAppError as exc:
        return exc.as_result()


VOICE_TOOLS = {
    "link_whatsapp": link_whatsapp_tool,
    "check_whatsapp": check_whatsapp_tool,
    "send_whatsapp": send_whatsapp_tool,
}


# --- CLI -----------------------------------------------------------------------------

def _cli(argv=None) -> int:
    import argparse

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    parser = argparse.ArgumentParser(description="Jarvis WhatsApp manager")
    parser.add_argument("--show", action="store_true", help="show the browser window")
    sub = parser.add_subparsers(dest="command", required=True)
    login = sub.add_parser("login", help="link WhatsApp once by scanning the QR code")
    login.add_argument("--timeout", type=float, default=180)
    sub.add_parser("status", help="check the saved login")
    read = sub.add_parser("read", help="summarise messages")
    read.add_argument("mode", nargs="?", default="unread", choices=FILTER_MODES)
    read.add_argument("--hours", type=float, default=DEFAULT_RECENT_HOURS)
    send = sub.add_parser("send", help="send a message")
    send.add_argument("recipient")
    send.add_argument("text")
    send.add_argument("--yes", action="store_true", help="don't ask before sending")
    args = parser.parse_args(argv)

    if args.command == "login":
        private = app_paths.redirected_home()
        if private is not None:
            print(
                "Not linking from here: Windows redirects this terminal's AppData writes to\n"
                f"  {private}\n"
                "(it was started from a packaged app, like the Claude desktop app), so the\n"
                "Jarvis you start from the Desktop would never see the login.\n"
                "Ask Jarvis instead: 'WhatsApp link karo' - or run this from a normal\n"
                "Windows terminal (Start menu > Terminal)."
            )
            return 2
    wa = WhatsAppManager(headless=not args.show)
    try:
        if args.command == "login":
            print("Scan the QR code in the browser window with WhatsApp on your phone "
                  "(Settings > Linked devices > Link a device).")
            result = wa.login(args.timeout)
        elif args.command == "status":
            result = wa.status()
        elif args.command == "read":
            result = wa.summarize_and_prompt_reply(args.mode, args.hours)
            for m in result.get("messages", []):
                print(f"  {m['time']:>9}  {m['sender']}: {m['text']}"
                      + (f"  ({m['unread']} unread)" if m["unread"] else ""))
        else:
            found = wa.find_recipient(args.recipient)
            if not args.yes:
                answer = input(f"Send to {found['recipient']}: {args.text!r}? [y/N] ")
                if answer.strip().lower() not in ("y", "yes"):
                    print("Not sent.")
                    return 1
            result = wa.send_message(found["target"], args.text)
    except WhatsAppError as exc:
        result = exc.as_result()
    finally:
        wa.close()
    print(result.get("message", result))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(_cli())
