"""RecitationAnalyzer: compare a recitation with the verified text.

Pipeline: microphone PCM -> Quran speech-recognition model (Tarteel's
Whisper fine-tune, run locally - audio never leaves the machine) ->
transcript -> word alignment against the expected ayahs -> feedback.

What it can judge: missing words, extra words, words said differently,
and within a word, letters that were dropped or replaced (e.g. ه for ح).

What it cannot judge, and says so: tajweed - elongation length, ghunnah,
qalqalah, heavy/light letters. A speech recogniser writes down *which*
letters it heard, not *how* they were pronounced, so presenting tajweed
feedback from it would be guessing. Low-confidence results (very quiet,
very short, or almost nothing matching) are reported as "couldn't hear
clearly", never as a child's mistake.
"""
from __future__ import annotations

import dataclasses
import difflib
import logging
import re
import threading

from .curriculum import LETTERS_BY_CHAR, confusion_hint

logger = logging.getLogger("jarvis.islamic.recitation")

ASR_MODEL = "tarteel-ai/whisper-base-ar-quran"
SAMPLE_RATE = 16000

# Diacritics and Quranic annotation marks: removed before comparing, since
# the recogniser and the Uthmani script mark vowels differently.
_MARKS = re.compile(
    "[\u0610-\u061a\u064b-\u065f\u0670\u06d6-\u06ed\u08d3-\u08ff\u0640\ufeff]"
)
_LETTER_MAP = str.maketrans({
    "ٱ": "ا", "أ": "ا", "إ": "ا", "آ": "ا", "ٲ": "ا", "ٳ": "ا",
    "ى": "ي", "ی": "ي", "ئ": "ي", "ؤ": "و", "ة": "ه", "ک": "ك",
})
# Spelling-only differences between Uthmani and everyday script: long-vowel
# alif is often not written in one of them ("الرحمن" / "الرحمان"), and a
# bare hamza may float. Letter feedback ignores these.
_SPELLING_ONLY = {"ا", "ء"}

MATCH, CLOSE, WRONG = 0.999, 0.8, 0.5


def normalize(text: str) -> str:
    text = _MARKS.sub("", text or "").translate(_LETTER_MAP)
    text = re.sub(r"[^ء-ي\s]", " ", text)
    return " ".join(text.split())


def skeleton(word: str) -> str:
    """A word reduced to what speech can actually distinguish."""
    return "".join(ch for ch in normalize(word) if ch not in _SPELLING_ONLY)


def words(text: str) -> list[str]:
    return normalize(text).split()


def similarity(a: str, b: str) -> float:
    a, b = skeleton(a), skeleton(b)
    if a == b:
        return 1.0
    if not a or not b:
        return 0.0
    return difflib.SequenceMatcher(None, a, b).ratio()


@dataclasses.dataclass(frozen=True)
class ExpectedWord:
    text: str          # original Uthmani word, for display
    ayah: int
    index: int         # position within its ayah (1-based)


@dataclasses.dataclass(frozen=True)
class WordResult:
    status: str        # correct | close | wrong | missing | extra
    expected: ExpectedWord | None
    heard: str | None
    score: float
    letter_issues: tuple[str, ...] = ()          # readable notes
    missed: tuple[str, ...] = ()                 # letters dropped
    confused: tuple[tuple[str, str], ...] = ()   # (expected, heard) letters

    def as_dict(self) -> dict:
        return {
            "status": self.status, "score": round(self.score, 2),
            "expected": self.expected.text if self.expected else None,
            "ayah": self.expected.ayah if self.expected else None,
            "heard": self.heard, "letter_issues": list(self.letter_issues),
        }


@dataclasses.dataclass
class RecitationResult:
    surah: int
    ayahs: tuple[int, int]
    transcript: str
    words: list[WordResult]
    accuracy: float
    clear: bool                    # False = too little to judge fairly
    letters_missed: dict[str, int]
    letters_confused: dict[str, int]   # "ح>ه": count  (expected > heard)

    @property
    def missing(self) -> list[WordResult]:
        return [w for w in self.words if w.status == "missing"]

    @property
    def mistakes(self) -> list[WordResult]:
        return [w for w in self.words if w.status in ("wrong", "missing")]

    def as_dict(self) -> dict:
        return {
            "surah": self.surah, "ayahs": list(self.ayahs), "transcript": self.transcript,
            "accuracy": round(self.accuracy, 2), "clear": self.clear,
            "words": [w.as_dict() for w in self.words],
            "letters_missed": self.letters_missed, "letters_confused": self.letters_confused,
        }


def expected_words(ayahs) -> list[ExpectedWord]:
    out = []
    for ayah in ayahs:
        for i, word in enumerate(ayah.arabic.split(), 1):
            if normalize(word):          # skip standalone pause marks
                out.append(ExpectedWord(word, ayah.ayah, i))
    return out


def _letter_issues(expected: str, heard: str):
    """Letters dropped or swapped inside one word (spelling-only letters ignored)."""
    missed, confused, notes = [], [], []
    a, b = skeleton(expected), skeleton(heard)
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b).get_opcodes():
        if op == "delete":
            missed.extend(a[i1:i2])
        elif op == "replace":
            exp, got = a[i1:i2], b[j1:j2]
            for k in range(max(len(exp), len(got))):
                e = exp[k] if k < len(exp) else None
                g = got[k] if k < len(got) else None
                if e and g:
                    confused.append((e, g))
                elif e:
                    missed.append(e)
    for letter in missed:
        name = LETTERS_BY_CHAR.get(letter)
        notes.append(f"missed the letter {letter}" + (f" ({name.name})" if name else ""))
    for e, g in confused:
        hint = confusion_hint(e, g)
        notes.append(f"said {g} instead of {e}" + (f" - {hint.rstrip('.')}" if hint else ""))
    return missed, confused, tuple(notes)


def _judge(exp: ExpectedWord, got: str, s: float) -> WordResult:
    if s >= MATCH:
        return WordResult("correct", exp, got, s)
    status = "close" if s >= CLOSE else "wrong"
    missed, confused, notes = _letter_issues(exp.text, got)
    return WordResult(status, exp, got, s, notes, tuple(missed), tuple(confused))


def align(expected: list[ExpectedWord], heard: list[str]) -> list[WordResult]:
    """Needleman-Wunsch over words, scored by skeleton similarity, so a
    skipped word or an extra word does not shift every later word into
    'wrong'.

    Uthmani script and everyday script don't always split words the same
    way: "يَٰٓأَيُّهَا" is one word in the Quran text but the recogniser writes
    "يا أيها". So one expected word may also match two heard words joined
    together, and two expected words one heard word.
    """
    n, m = len(expected), len(heard)
    gap = -0.6
    gain = lambda s: s * 2 - 0.8 if s >= WRONG else -1.5
    sims = [[similarity(e.text, h) for h in heard] for e in expected]
    score = [[0.0] * (m + 1) for _ in range(n + 1)]
    back = [[""] * (m + 1) for _ in range(n + 1)]
    for i in range(1, n + 1):
        score[i][0], back[i][0] = i * gap, "up"
    for j in range(1, m + 1):
        score[0][j], back[0][j] = j * gap, "left"
    for i in range(1, n + 1):
        for j in range(1, m + 1):
            options = [
                (score[i - 1][j - 1] + gain(sims[i - 1][j - 1]), "diag"),
                (score[i - 1][j] + gap, "up"),
                (score[i][j - 1] + gap, "left"),
            ]
            if j >= 2:   # one expected word, said/written as two
                # exact only: script differences keep every letter; a near
                # match here is a real mistake and must not be glued away
                s2 = similarity(expected[i - 1].text, heard[j - 2] + heard[j - 1])
                if s2 >= MATCH:
                    options.append((score[i - 1][j - 2] + gain(s2) + 0.1, "merge"))
            if i >= 2:   # two expected words, written as one
                s2 = similarity(expected[i - 2].text + expected[i - 1].text, heard[j - 1])
                if s2 >= MATCH:
                    options.append((score[i - 2][j - 1] + gain(s2) + 0.1, "split"))
            score[i][j], back[i][j] = max(options, key=lambda o: o[0])
    out: list[WordResult] = []
    i, j = n, m
    while i > 0 or j > 0:
        move = back[i][j] if i > 0 and j > 0 else ("up" if i > 0 else "left")
        if move == "diag":
            out.append(_judge(expected[i - 1], heard[j - 1], sims[i - 1][j - 1]))
            i, j = i - 1, j - 1
        elif move == "merge":
            got = heard[j - 2] + heard[j - 1]
            out.append(_judge(expected[i - 1], got, similarity(expected[i - 1].text, got)))
            i, j = i - 1, j - 2
        elif move == "split":
            pair = expected[i - 2].text + expected[i - 1].text
            got = heard[j - 1]
            s = similarity(pair, got)
            status = "correct" if s >= MATCH else "close" if s >= CLOSE else "wrong"
            # letter issues belong to the pair - report them once, on the first word
            missed, confused, notes = ((), (), ()) if status == "correct" else _letter_issues(pair, got)
            out.append(WordResult(status, expected[i - 1], got, s))
            out.append(WordResult(status, expected[i - 2], got, s, notes, tuple(missed), tuple(confused)))
            i, j = i - 2, j - 1
        elif move == "up":
            out.append(WordResult("missing", expected[i - 1], None, 0.0))
            i -= 1
        else:
            out.append(WordResult("extra", None, heard[j - 1], 0.0))
            j -= 1
    out.reverse()
    return out


def analyze(transcript: str, ayahs, audio_seconds: float | None = None) -> RecitationResult:
    exp = expected_words(ayahs)
    heard = words(transcript)
    results = align(exp, heard)
    scored = [w for w in results if w.expected is not None]
    credit = {"correct": 1.0, "close": 0.75, "wrong": 0.25, "missing": 0.0}
    accuracy = sum(credit[w.status] for w in scored) / max(1, len(scored))
    matched = sum(1 for w in scored if w.status in ("correct", "close"))
    # Fairness gate: if we barely heard anything that lines up, the problem
    # is almost certainly the microphone or background noise, not the reciter.
    too_short = audio_seconds is not None and audio_seconds < 0.6
    clear = bool(heard) and not too_short and matched / max(1, len(exp)) >= 0.3

    missed, confused = {}, {}
    for w in results:
        for letter in w.missed:
            missed[letter] = missed.get(letter, 0) + 1
        for e, g in w.confused:
            confused[f"{e}>{g}"] = confused.get(f"{e}>{g}", 0) + 1
    return RecitationResult(
        surah=ayahs[0].surah, ayahs=(ayahs[0].ayah, ayahs[-1].ayah), transcript=transcript,
        words=results, accuracy=accuracy, clear=clear,
        letters_missed=missed, letters_confused=confused,
    )


TAJWEED_NOTE = ("I checked the words and letters; I can't judge tajweed (elongation, "
                "ghunnah, qalqalah) from a transcript - a teacher should check those.")


def feedback(result: RecitationResult, child: bool, name: str | None = None) -> str:
    """Spoken-style feedback. Children get praise first and at most two
    gentle corrections; adults get the full list."""
    who = f"{name}, " if name else ""
    if not result.clear:
        return (f"{who}I couldn't hear the recitation clearly enough to check it. "
                "Let's try again - a bit closer to the microphone, in a quiet room.")
    total = len([w for w in result.words if w.expected])
    good = len([w for w in result.words if w.status == "correct"])
    missing = result.missing
    wrong = [w for w in result.words if w.status == "wrong"]
    close = [w for w in result.words if w.status == "close"]
    extra = [w for w in result.words if w.status == "extra"]
    pct = round(result.accuracy * 100)

    if child:
        from .curriculum import ENCOURAGEMENT, POSITIVE_FEEDBACK
        import random
        if result.accuracy >= 0.95:
            return f"{who}{random.choice(POSITIVE_FEEDBACK)} You recited every word correctly!"
        lines = [f"{who}{random.choice(POSITIVE_FEEDBACK if result.accuracy >= 0.7 else ENCOURAGEMENT)}",
                 f"You said {good} of {total} words just right."]
        fixes = (missing + wrong + close)[:2]
        for w in fixes:
            if w.status == "missing":
                lines.append(f"Don't forget the word {w.expected.text} in ayah {w.expected.ayah}.")
            else:
                issue = w.letter_issues[0] if w.letter_issues else "listen to how it sounds"
                lines.append(f"Let's practise {w.expected.text} (ayah {w.expected.ayah}) - {issue}.")
        lines.append("Let's listen once more and try again!")
        return " ".join(lines)

    lines = [f"{who}accuracy {pct}% - {good} of {total} words exactly right."]
    if missing:
        lines.append("Missing words: " + ", ".join(
            f"{w.expected.text} (ayah {w.expected.ayah})" for w in missing[:6]) + ".")
    for w in (wrong + close)[:6]:
        issues = "; ".join(w.letter_issues[:2]) or "pronounced differently"
        lines.append(f"{w.expected.text} (ayah {w.expected.ayah}): heard {w.heard} - {issues}.")
    if extra:
        lines.append("Extra words heard: " + ", ".join(w.heard for w in extra[:4]) + ".")
    if not (missing or wrong or close):
        lines.append("Every word was right.")
    lines.append(TAJWEED_NOTE)
    return " ".join(lines)


class Transcriber:
    """Local Quran speech recogniser, loaded lazily and kept warm.

    transformers + torch are optional dependencies. Without them
    `available()` is False and callers explain how to enable checking.
    """

    def __init__(self, model_name: str = ASR_MODEL):
        self.model_name = model_name
        self._lock = threading.Lock()
        self._model = None
        self._processor = None
        self.error: str | None = None

    def available(self) -> bool:
        try:
            import torch  # noqa: F401
            import transformers  # noqa: F401
        except ImportError:
            self.error = "pip install torch transformers"
            return False
        return True

    def load(self) -> bool:
        with self._lock:
            if self._model is not None:
                return True
            if not self.available():
                return False
            try:
                import torch
                from transformers import WhisperForConditionalGeneration, WhisperProcessor
                processor = WhisperProcessor.from_pretrained(self.model_name)
                model = WhisperForConditionalGeneration.from_pretrained(self.model_name).eval()
                # This fine-tune ships with the decoder KV cache switched off,
                # which makes decoding ~2.5x slower on CPU; int8 weights add more.
                model.config.use_cache = True
                model.generation_config.use_cache = True
                model = torch.ao.quantization.quantize_dynamic(
                    model, {torch.nn.Linear}, dtype=torch.qint8)
                self._processor, self._model = processor, model
            except Exception as exc:          # download/disk/torch problems
                logger.exception("could not load %s", self.model_name)
                self.error = str(exc)
                return False
            return True

    def transcribe(self, pcm16: bytes, sample_rate: int = SAMPLE_RATE) -> str:
        import numpy as np
        import torch
        if not self.load():
            raise RuntimeError(f"speech model unavailable: {self.error}")
        audio = np.frombuffer(pcm16, dtype="<i2").astype("float32") / 32768.0
        if sample_rate != SAMPLE_RATE:
            x = np.linspace(0, len(audio), int(len(audio) * SAMPLE_RATE / sample_rate), endpoint=False)
            audio = np.interp(x, np.arange(len(audio)), audio).astype("float32")
        texts = []
        # Whisper hears 30 s at a time; longer recitations go in chunks.
        step = SAMPLE_RATE * 28
        for start in range(0, max(1, len(audio)), step):
            chunk = audio[start:start + step]
            if len(chunk) < SAMPLE_RATE // 4:
                continue
            feats = self._processor(chunk, sampling_rate=SAMPLE_RATE,
                                    return_tensors="pt").input_features
            with torch.inference_mode():
                ids = self._model.generate(feats, num_beams=1, max_new_tokens=220, use_cache=True)
            text = self._processor.batch_decode(ids, skip_special_tokens=True)[0]
            texts.append(re.sub(r"<\|[^|>]*\|>", "", text))   # leaked <|ar|> style tokens
        return " ".join(t.strip() for t in texts if t.strip())
