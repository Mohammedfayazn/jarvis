"""PronunciationAnalyzer: did the speech model hear the word we practised?

A local multilingual Whisper model (openai/whisper-base) transcribes the
child's attempt with the language fixed, and the transcript is compared
with the expected word. When they differ, the letters that changed point
to the sound being practised (r -> w in "wabbit", y -> l in "lellow").

What this can and can't tell, measured on test words before shipping:
  * substitutions that make a non-word come through ("wabbit" -> "Wobbit",
    "lellow" -> "Lolo", "nana" for "banana") - these are reported;
  * substitutions that make another real word are silently "corrected" by
    the model ("thun" -> "Sun"), so a lisp can pass unnoticed;
  * young children's speech is harder for these models than adults'.
So a single miss is never called a problem: the child hears "good try,
let's say it together", and a sound only shows up as "keep practising"
in the parent dashboard after repeated attempts. Not a diagnostic tool.
"""
from __future__ import annotations

import dataclasses
import difflib
import logging
import re
import threading
import unicodedata

from .vocabulary import Word, sound_spans

logger = logging.getLogger("jarvis.speech_coach")

ASR_MODEL = "openai/whisper-base"
SAMPLE_RATE = 16000
WHISPER_LANG = {"en": "en", "nl": "nl", "hi": "hi"}
MATCH, CLOSE = 0.999, 0.6


def _clean(text: str, language: str) -> str:
    text = unicodedata.normalize("NFC", (text or "").lower())
    text = re.sub(r"<\|[^|>]*\|>", "", text)
    text = re.sub(r"[^\wऀ-ॿ ]", " ", text)
    words = text.split()
    articles = {"en": {"the", "a", "an"}, "nl": {"de", "het", "een"}}.get(language, set())
    words = [w for w in words if w not in articles]
    return " ".join(words)


@dataclasses.dataclass(frozen=True)
class Attempt:
    word: Word
    language: str
    expected: str
    heard: str
    score: float
    status: str                         # clear | close | different | unclear
    sounds_ok: tuple[str, ...]
    sounds_missed: tuple[str, ...]

    @property
    def recognized(self) -> bool:
        return self.status == "clear"

    def as_dict(self) -> dict:
        return {"word": self.word.key, "language": self.language, "expected": self.expected,
                "heard": self.heard, "score": round(self.score, 2), "status": self.status,
                "sounds_ok": list(self.sounds_ok), "sounds_missed": list(self.sounds_missed)}


def compare(word: Word, language: str, transcript: str) -> Attempt:
    expected = _clean(word.text(language), language)
    heard = _clean(transcript, language)
    if not heard:
        return Attempt(word, language, expected, "", 0.0, "unclear", (), ())
    # the target word anywhere in a short answer counts ("a dog!", "it's a dog")
    if expected in heard.split() or heard == expected:
        spans = sound_spans(expected, language)
        return Attempt(word, language, expected, heard, 1.0, "clear",
                       tuple(s for s, _, _ in spans), ())
    best = max(heard.split() + [heard], key=lambda h: difflib.SequenceMatcher(None, expected, h).ratio())
    score = difflib.SequenceMatcher(None, expected, best).ratio()
    if score < CLOSE:
        # too different to say anything about sounds - probably unclear audio
        return Attempt(word, language, expected, heard, score, "different", (), ())
    changed = set()
    for op, i1, i2, _j1, _j2 in difflib.SequenceMatcher(None, expected, best).get_opcodes():
        if op in ("replace", "delete"):
            changed.update(range(i1, i2))
    spans = sound_spans(expected, language)
    missed = tuple(s for s, a, b in spans if changed & set(range(a, b)))
    ok = tuple(s for s, a, b in spans if not changed & set(range(a, b)))
    return Attempt(word, language, expected, best, score, "close", ok, missed)


FEEDBACK = {
    "en": {"clear": "Yes! {word} - beautiful!", "close": "Good try! Let's say it together: {word}.",
           "different": "Good try! Listen: {word}. Now you!",
           "unclear": "I couldn't hear you well. Can you say {word} again, a bit louder?",
           "sound": " Let's practise the '{sound}' sound together."},
    "nl": {"clear": "Ja! {word} - heel goed!", "close": "Goed geprobeerd! Samen: {word}.",
           "different": "Goed geprobeerd! Luister: {word}. Nu jij!",
           "unclear": "Ik hoorde je niet goed. Zeg nog eens {word}, iets harder?",
           "sound": " Laten we de '{sound}' samen oefenen."},
    "hi": {"clear": "हाँ! {word} - बहुत बढ़िया!", "close": "अच्छी कोशिश! साथ में बोलो: {word}।",
           "different": "अच्छी कोशिश! सुनो: {word}। अब तुम!",
           "unclear": "मैं ठीक से सुन नहीं पाया। {word} फिर से बोलो, थोड़ा ज़ोर से?",
           "sound": ""},
}


def feedback(attempt: Attempt) -> str:
    """Always kind; names at most one sound to practise."""
    lines = FEEDBACK[attempt.language]
    text = lines[attempt.status].format(word=attempt.word.say(attempt.language))
    if attempt.status == "close" and attempt.sounds_missed and lines["sound"]:
        text += lines["sound"].format(sound=attempt.sounds_missed[0])
    return text


class MultilingualTranscriber:
    """Local Whisper, loaded lazily; the audio never leaves the computer."""

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
                model = torch.ao.quantization.quantize_dynamic(model, {torch.nn.Linear}, dtype=torch.qint8)
                self._processor, self._model = processor, model
            except Exception as exc:
                logger.exception("could not load %s", self.model_name)
                self.error = str(exc)
                return False
            return True

    def transcribe(self, pcm16: bytes, language: str) -> str:
        import numpy as np
        import torch
        if not self.load():
            raise RuntimeError(f"speech model unavailable: {self.error}")
        audio = np.frombuffer(pcm16, dtype="<i2").astype("float32") / 32768.0
        audio = audio[: SAMPLE_RATE * 25]
        feats = self._processor(audio, sampling_rate=SAMPLE_RATE, return_tensors="pt").input_features
        with torch.inference_mode():
            ids = self._model.generate(feats, language=WHISPER_LANG[language], task="transcribe",
                                       num_beams=1, max_new_tokens=40)
        return self._processor.batch_decode(ids, skip_special_tokens=True)[0].strip()
