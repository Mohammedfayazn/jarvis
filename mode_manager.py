"""Who Jarvis is talking to - Fayaz (Adult Mode) or his 4-year-old daughter
(Child Mode) - and everything that has to change with it.

    "Jarvis, speak to my daughter" / "kids mode"  ->  Child Mode
    "Jarvis, back to me" / "adult mode"           ->  Adult Mode

A mode is a `Profile`: the system prompt, how fast Jarvis speaks, how long
it waits for the speaker to finish, and which tools it may use. Switching
reconnects the Live session (the pause threshold is part of the connect
config) and starts a fresh conversation, so the adult talk isn't in the
child's context. The switch is a tool (`set_mode`) the model calls; the
spoken phrases are also matched here in `detect`, so a mode change still
works when the model misses the tool.

What each mode changes, and how real each part is:

* Pause threshold - REAL: `silence_duration_ms` in the Live API's automatic
  activity detection. 1.2 s for an adult, 4.0 s for a child, who stops
  mid-sentence to think and must not be interrupted. The recorder used for
  word practice and recitation (audio_runtime.UtteranceRecorder) gets the
  same floor through `record_pause`.
* Tools - REAL: Child Mode only offers the coach and Quran tools. A child
  cannot send a WhatsApp message or close windows by saying so.
* Speaking rate - GUIDANCE ONLY: the Live API has no speaking-rate setting
  (`SpeechConfig` is voice and language), and the audio arrives as finished
  speech. The prompt asks for ~120 words per minute in Child Mode against
  ~180 in Adult Mode, with one short sentence and then a pause. The model
  follows that loosely; it is not an exact rate, and nothing here can make
  it one without re-stretching the audio.
"""
from __future__ import annotations

import logging
import re
import threading
import unicodedata
from dataclasses import dataclass, field

from google.genai import types

import prompts

log = logging.getLogger("jarvis.mode")

ADULT = "adult"
CHILD = "child"

# Tools Child Mode offers: the speech coach and the Quran tutor, plus the
# way back. Everything else (WhatsApp, windows, projects, memory, YouTube)
# is left out of the session - a child asking for it can't set it off.
CHILD_TOOL_NAMES = frozenset({
    "speech_coach", "coach_words", "practice_word", "story_time", "sentence_practice",
    "quran_lesson", "lesson_done", "quran_quiz", "test_recitation",
    "play_quran", "stop_quran_audio",
    "set_mode", "go_to_sleep",
})


@dataclass(frozen=True)
class Profile:
    """One user profile - a persona plus its speech settings."""

    key: str
    label: str                       # for the console and the HUD
    system_prompt: str
    words_per_minute: int            # guidance for the prompt, see module docstring
    pause_seconds: float             # silence that ends a turn
    prefix_padding_ms: int           # audio kept from before speech starts
    record_pause: float              # floor for UtteranceRecorder pauses
    end_sensitivity: types.EndSensitivity
    greeting: str                    # what Jarvis does first after switching
    tool_names: frozenset[str] | None = None      # None = every tool

    @property
    def silence_duration_ms(self) -> int:
        return int(self.pause_seconds * 1000)

    def allows(self, tool_name: str) -> bool:
        return self.tool_names is None or tool_name in self.tool_names


def _pacing(words_per_minute: int, sentences: str) -> str:
    return (
        f"\n\nSPEAKING PACE: speak at about {words_per_minute} words per minute. "
        f"{sentences}"
    )


ADULT_PROFILE = Profile(
    key=ADULT,
    label="Adult Mode (Fayaz)",
    system_prompt=prompts.ADULT_SYSTEM_PROMPT + _pacing(
        180, "Normal conversational speed - concise and efficient."),
    words_per_minute=180,
    pause_seconds=1.2,
    prefix_padding_ms=300,
    record_pause=1.5,
    end_sensitivity=types.EndSensitivity.END_SENSITIVITY_HIGH,
    greeting="[Adult Mode. Say one short line to Fayaz that you're back - nothing else.]",
)

CHILD_PROFILE = Profile(
    key=CHILD,
    label="Child Mode (Zunaira, 4)",
    system_prompt=prompts.CHILD_SYSTEM_PROMPT + _pacing(
        120, "Slow and gentle. One short sentence, then STOP and wait for her."),
    words_per_minute=120,
    pause_seconds=4.0,
    prefix_padding_ms=600,
    # A 4-year-old pauses mid-sentence to think; wait rather than cut in
    record_pause=4.0,
    end_sensitivity=types.EndSensitivity.END_SENSITIVITY_LOW,
    greeting=(
        "[Child Mode. Greet Zunaira warmly in ONE short, simple sentence and ask "
        "if she wants to play with words or read Quran. Nothing else.]"
    ),
    tool_names=CHILD_TOOL_NAMES,
)

PROFILES = {ADULT: ADULT_PROFILE, CHILD: CHILD_PROFILE}


# --- Spoken phrases ---------------------------------------------------------

def _normalise(text: str) -> str:
    text = unicodedata.normalize("NFKC", text or "").casefold()
    return " ".join(re.sub(r"[^\w\sऀ-ॿ]+", " ", text).split())


# Deliberately specific: "child" or "baat karo" alone must never switch mode.
_TO_CHILD = [
    r"\b(speak|talk|baat)\s+(to|with|karo?\s+(se|with))?\s*(my\s+)?(daughter|beti|bachch?i|zunaira)\b",
    r"\b(zunaira|beti|bachch?i)\s+(se|ke saath)\s+baat\b",
    r"\b(activate|start|switch to|turn on|chalu karo)?\s*(kids?|child|bachch?a|bachch?i)\s*mode\b",
    r"\bmode\s*(kids?|child)\b",
    r"बेटी से बात|बच्ची से बात|ज़ुनैरा से बात",
]
_TO_ADULT = [
    r"\b(back to me|talk to me again|mujh?se baat karo|wapas mere paas)\b",
    r"\b(activate|start|switch to|turn on|wapas)?\s*(adult|normal|grown[- ]?up|bada)\s*mode\b",
    r"\bmode\s*(adult|normal)\b",
    r"\b(stop|band karo|exit|off)\s*(the\s+)?(kids?|child)\s*mode\b",
    r"मुझसे बात करो|वापस मेरे पास",
]
_TO_CHILD_RE = [re.compile(p) for p in _TO_CHILD]
_TO_ADULT_RE = [re.compile(p) for p in _TO_ADULT]


def detect(text: str) -> str | None:
    """The mode a spoken line asks for, or None.

    Adult wins when a line somehow matches both ("back to me" while the
    word "daughter" is in the same sentence): going back to the adult must
    never be the request that gets dropped.
    """
    said = _normalise(text)
    if not said:
        return None
    if any(r.search(said) for r in _TO_ADULT_RE):
        return ADULT
    if any(r.search(said) for r in _TO_CHILD_RE):
        return CHILD
    return None


# --- The manager ------------------------------------------------------------

class ModeManager:
    """The current profile, and the switch that makes the session reconnect.

    Thread-safe: the `set_mode` tool runs on a worker thread, the session
    loop on the asyncio loop. `on_switch` is called (on the caller's thread)
    whenever the mode actually changes - main.py uses it to wake the
    session loop, which reconnects with the new profile.
    """

    def __init__(self, on_switch=None, profile: Profile = ADULT_PROFILE):
        self._lock = threading.Lock()
        self._profile = profile
        self._on_switch = on_switch
        self._greeting: str | None = None

    @property
    def profile(self) -> Profile:
        with self._lock:
            return self._profile

    @property
    def key(self) -> str:
        return self.profile.key

    def switch(self, target: str | None) -> dict:
        """Change mode. Switching to the mode already active does nothing."""
        target = (target or "").strip().lower()
        if target in ("kid", "kids", "daughter", "zunaira"):
            target = CHILD
        if target not in PROFILES:
            return {"ok": False, "mode": self.key,
                    "message": f"Mode '{target}' nahi hai - adult ya child."}

        with self._lock:
            if self._profile.key == target:
                return {"ok": True, "mode": target, "changed": False,
                        "message": f"{self._profile.label} pehle se chal raha hai."}
            self._profile = PROFILES[target]
            self._greeting = self._profile.greeting
            profile = self._profile

        log.info("Mode -> %s (pause %.1fs, ~%d wpm)",
                 profile.label, profile.pause_seconds, profile.words_per_minute)
        if self._on_switch is not None:
            self._on_switch(profile)
        return {
            "ok": True, "mode": profile.key, "changed": True,
            "message": (f"{profile.label} chalu. Naya session ban raha hai - "
                        "ek pal me tayyar."),
        }

    def take_greeting(self) -> str | None:
        """What to open the new session with - once, after a switch."""
        with self._lock:
            greeting, self._greeting = self._greeting, None
            return greeting

    # ---- what the rest of Jarvis asks for ----

    def record_pause(self, requested: float) -> float:
        """Never cut a speaker off sooner than their profile allows."""
        return max(float(requested), self.profile.record_pause)

    def tools(self, all_tools: list[types.Tool]) -> list[types.Tool]:
        """`all_tools` with anything this profile doesn't allow removed."""
        profile = self.profile
        if profile.tool_names is None:
            return all_tools
        kept = []
        for tool in all_tools:
            allowed = [d for d in (tool.function_declarations or [])
                       if profile.allows(d.name)]
            if allowed:
                kept.append(tool.model_copy(update={"function_declarations": allowed}))
        return kept

    def live_config(self, base: types.LiveConnectConfig,
                    extra_instruction: str = "") -> types.LiveConnectConfig:
        """`base` with this profile's prompt, pause threshold and tools."""
        profile = self.profile
        text = profile.system_prompt + (("\n\n" + extra_instruction) if extra_instruction else "")
        return base.model_copy(update={
            "system_instruction": types.Content(parts=[types.Part(text=text)]),
            "realtime_input_config": types.RealtimeInputConfig(
                automatic_activity_detection=types.AutomaticActivityDetection(
                    silence_duration_ms=profile.silence_duration_ms,
                    prefix_padding_ms=profile.prefix_padding_ms,
                    end_of_speech_sensitivity=profile.end_sensitivity,
                ),
            ),
            "tools": self.tools(list(base.tools or [])),
        })
