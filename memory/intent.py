"""Rule-based understanding of memory-related sentences.

In the live assistant, Jarvis's own Gemini call already extracts key/value
when it decides to call remember_this (see main.py's TOOLS - Gemini fills
the arguments the same way it does for play_on_youtube's query). This
module exists for the parts that need understanding independent of a
Gemini round-trip: standalone/CLI use, tests, and detecting an unprompted
storable fact so Jarvis can ask before saving it, rather than silently
storing something it merely overheard.
"""
from __future__ import annotations

import dataclasses
import re

from .models import normalize_category

REMEMBER_PREFIX = re.compile(
    r"^(?:hey\s+jarvis|jarvis)?[,:]?\s*remember\s+(?:that\s+)?", re.IGNORECASE
)

QUESTION_WORDS = re.compile(
    r"^(what|when|where|who|which|how|do\s+you\s+know|can\s+you\s+tell)\b",
    re.IGNORECASE,
)

# Tried in order; first one that matches a statement wins. Grouped so
# "starts at" is checked before the generic "is/are" pattern would
# otherwise mis-split it.
_PATTERNS = [
    re.compile(r"^my\s+(?P<key>.+?)\s+(?:is|are)\s+(?P<value>.+?)[.!]?$", re.IGNORECASE),
    re.compile(
        r"^my\s+(?P<key>.+?)\s+(?:starts?|begins?)\s+at\s+(?P<value>.+?)[.!]?$",
        re.IGNORECASE,
    ),
    re.compile(r"^i\s+live\s+in\s+(?P<value>.+?)[.!]?$", re.IGNORECASE),
    re.compile(r"^i\s+work\s+(?:at|for)\s+(?P<value>.+?)[.!]?$", re.IGNORECASE),
    re.compile(r"^(?P<key>.+?)\s+(?:is|are)\s+(?P<value>.+?)[.!]?$", re.IGNORECASE),
]

_CATEGORY_HINTS = [
    (re.compile(r"\b(daughter|son|wife|husband|mother|father|mom|dad|family|kids?)\b", re.I), "Family"),
    (re.compile(r"\b(favorite|favourite|prefer|like|hobby)\b", re.I), "Preferences"),
    (re.compile(r"\b(office|work|meeting|boss|job|colleague|salary)\b", re.I), "Work"),
    (re.compile(r"\b(project|deadline|repo|sprint|release|client)\b", re.I), "Projects"),
    (re.compile(r"\b(remind|reminder|appointment|anniversary|birthday)\b", re.I), "Reminders"),
]

_MAX_EXTRACTION_WORDS = 12  # longer sentences are conversation, not a short fact


@dataclasses.dataclass(frozen=True)
class MemoryIntent:
    action: str  # "remember" | "query" | "extract_candidate" | "none"
    category: str | None = None
    key: str | None = None
    value: str | None = None
    query: str | None = None


def guess_category(text: str) -> str:
    for pattern, category in _CATEGORY_HINTS:
        if pattern.search(text):
            return category
    return normalize_category(None)


def _extract_key_value(statement: str):
    statement = statement.strip()
    for pattern in _PATTERNS:
        m = pattern.match(statement)
        if not m:
            continue
        groups = m.groupdict()
        value = groups["value"].strip()
        key = groups.get("key", "").strip() if groups.get("key") else None
        if not key:
            key = "where I live" if "live" in statement.lower() else "where I work"
        if key and value:
            return key, value
    return None


def parse(text: str) -> MemoryIntent:
    """Classify a sentence: an explicit remember command, a question that
    might be answered from memory, or neither."""
    stripped = text.strip()
    if not stripped:
        return MemoryIntent(action="none")

    m = REMEMBER_PREFIX.match(stripped)
    if m:
        statement = stripped[m.end():].strip()
        extracted = _extract_key_value(statement)
        if extracted:
            key, value = extracted
            return MemoryIntent(
                action="remember", category=guess_category(statement),
                key=key, value=value,
            )
        return MemoryIntent(action="remember", value=statement)

    if QUESTION_WORDS.match(stripped) or stripped.endswith("?"):
        return MemoryIntent(action="query", query=stripped)

    return MemoryIntent(action="none")


def detect_storable_fact(text: str) -> MemoryIntent | None:
    """Notice a declarative personal-fact sentence that was NOT explicitly
    prefixed with "remember" - e.g. "My favorite programming language is
    Python." Returns an extract_candidate intent so the caller can ask
    "Would you like me to remember that?" instead of storing anything on
    its own initiative.
    """
    stripped = text.strip()
    if not stripped or REMEMBER_PREFIX.match(stripped):
        return None
    if QUESTION_WORDS.match(stripped) or stripped.endswith("?"):
        return None
    extracted = _extract_key_value(stripped)
    if not extracted:
        return None
    key, value = extracted
    if len(value.split()) > _MAX_EXTRACTION_WORDS:
        return None
    return MemoryIntent(
        action="extract_candidate", category=guess_category(stripped),
        key=key, value=value,
    )
