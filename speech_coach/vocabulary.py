"""VocabularyTrainer: everyday words in English, Dutch and Hindi.

Six categories of eight picture-able words, each with an emoji (the
"picture"), the Dutch article (de/het - it matters at a Dutch school), and
Hindi in Devanagari plus a romanised form a parent can read aloud.

The Dutch and Hindi here were written for this module, not taken from a
dictionary API: parents, please correct anything that sounds off in your
family's usage - it's plain data in this file.
"""
from __future__ import annotations

import dataclasses
import re

LANGUAGES = ("en", "nl", "hi")
LANGUAGE_NAMES = {"en": "English", "nl": "Dutch", "hi": "Hindi"}
CATEGORIES = ("animals", "food", "school", "family", "colors", "nature")


@dataclasses.dataclass(frozen=True)
class Word:
    key: str
    category: str
    emoji: str
    en: str
    nl: str
    nl_article: str | None      # "de" / "het" / None for colours
    hi: str                     # Devanagari
    hi_roman: str

    def text(self, language: str, with_article: bool = False) -> str:
        if language == "nl":
            return f"{self.nl_article} {self.nl}" if with_article and self.nl_article else self.nl
        if language == "hi":
            return self.hi
        return self.en

    def say(self, language: str) -> str:
        """How Jarvis names it to the child (Hindi romanised for reading aloud)."""
        if language == "hi":
            return f"{self.hi} ({self.hi_roman})"
        return self.text(language, with_article=True)

    def as_dict(self) -> dict:
        return dataclasses.asdict(self)


def _w(key, category, emoji, en, nl, article, hi, hi_roman) -> Word:
    return Word(key, category, emoji, en, nl, article, hi, hi_roman)


WORDS: tuple[Word, ...] = (
    # animals
    _w("dog", "animals", "🐶", "dog", "hond", "de", "कुत्ता", "kutta"),
    _w("cat", "animals", "🐱", "cat", "kat", "de", "बिल्ली", "billi"),
    _w("cow", "animals", "🐄", "cow", "koe", "de", "गाय", "gaay"),
    _w("bird", "animals", "🐦", "bird", "vogel", "de", "चिड़िया", "chidiya"),
    _w("fish", "animals", "🐟", "fish", "vis", "de", "मछली", "machhli"),
    _w("horse", "animals", "🐴", "horse", "paard", "het", "घोड़ा", "ghoda"),
    _w("rabbit", "animals", "🐰", "rabbit", "konijn", "het", "खरगोश", "khargosh"),
    _w("elephant", "animals", "🐘", "elephant", "olifant", "de", "हाथी", "haathi"),
    # food
    _w("apple", "food", "🍎", "apple", "appel", "de", "सेब", "seb"),
    _w("banana", "food", "🍌", "banana", "banaan", "de", "केला", "kela"),
    _w("bread", "food", "🍞", "bread", "brood", "het", "ब्रेड", "bread"),
    _w("milk", "food", "🥛", "milk", "melk", "de", "दूध", "doodh"),
    _w("water", "food", "💧", "water", "water", "het", "पानी", "paani"),
    _w("egg", "food", "🥚", "egg", "ei", "het", "अंडा", "anda"),
    _w("rice", "food", "🍚", "rice", "rijst", "de", "चावल", "chaawal"),
    _w("carrot", "food", "🥕", "carrot", "wortel", "de", "गाजर", "gaajar"),
    # school
    _w("book", "school", "📖", "book", "boek", "het", "किताब", "kitaab"),
    _w("pencil", "school", "✏️", "pencil", "potlood", "het", "पेंसिल", "pencil"),
    _w("teacher", "school", "🧑‍🏫", "teacher", "juf", "de", "टीचर", "teacher"),
    _w("bag", "school", "🎒", "bag", "tas", "de", "बस्ता", "basta"),
    _w("chair", "school", "🪑", "chair", "stoel", "de", "कुर्सी", "kursi"),
    _w("table", "school", "🍽️", "table", "tafel", "de", "मेज़", "mez"),
    _w("ball", "school", "⚽", "ball", "bal", "de", "गेंद", "gend"),
    _w("friend", "school", "🧒", "friend", "vriendje", "het", "दोस्त", "dost"),
    # family
    _w("mother", "family", "👩", "mum", "mama", "de", "माँ", "maa"),
    _w("father", "family", "👨", "dad", "papa", "de", "पापा", "papa"),
    _w("sister", "family", "👧", "sister", "zus", "de", "बहन", "behen"),
    _w("brother", "family", "👦", "brother", "broer", "de", "भाई", "bhai"),
    _w("baby", "family", "👶", "baby", "baby", "de", "बच्चा", "bachcha"),
    _w("grandma", "family", "👵", "grandma", "oma", "de", "दादी", "daadi"),
    _w("grandpa", "family", "👴", "grandpa", "opa", "de", "दादा", "daada"),
    _w("family", "family", "👨‍👩‍👧", "family", "familie", "de", "परिवार", "parivaar"),
    # colors
    _w("red", "colors", "🔴", "red", "rood", None, "लाल", "laal"),
    _w("blue", "colors", "🔵", "blue", "blauw", None, "नीला", "neela"),
    _w("green", "colors", "🟢", "green", "groen", None, "हरा", "hara"),
    _w("yellow", "colors", "🟡", "yellow", "geel", None, "पीला", "peela"),
    _w("black", "colors", "⚫", "black", "zwart", None, "काला", "kaala"),
    _w("white", "colors", "⚪", "white", "wit", None, "सफ़ेद", "safed"),
    _w("orange", "colors", "🟠", "orange", "oranje", None, "नारंगी", "naarangi"),
    _w("pink", "colors", "🩷", "pink", "roze", None, "गुलाबी", "gulaabi"),
    # nature
    _w("sun", "nature", "☀️", "sun", "zon", "de", "सूरज", "sooraj"),
    _w("moon", "nature", "🌙", "moon", "maan", "de", "चाँद", "chaand"),
    _w("tree", "nature", "🌳", "tree", "boom", "de", "पेड़", "ped"),
    _w("flower", "nature", "🌸", "flower", "bloem", "de", "फूल", "phool"),
    _w("rain", "nature", "🌧️", "rain", "regen", "de", "बारिश", "baarish"),
    _w("star", "nature", "⭐", "star", "ster", "de", "तारा", "taara"),
    _w("sea", "nature", "🌊", "sea", "zee", "de", "समुद्र", "samudra"),
    _w("cloud", "nature", "☁️", "cloud", "wolk", "de", "बादल", "baadal"),
)
WORDS_BY_KEY = {w.key: w for w in WORDS}

# Sounds worth tracking, spotted from spelling (longest first). A word
# "practises" a sound when its spelling contains it; that is how difficult
# sounds are counted without hand-tagging every word.
SOUNDS = {
    "en": ("th", "sh", "ch", "r", "l", "s", "y", "v", "w", "f", "k", "g", "b", "p"),
    "nl": ("sch", "ch", "ij", "ei", "ui", "oe", "eu", "g", "r", "w", "v", "z", "s", "l"),
}


def sound_spans(word: str, language: str) -> list[tuple[str, int, int]]:
    """[(sound, start, end)] for the tracked sounds in a word's spelling."""
    text = word.lower()
    taken = [False] * len(text)
    spans = []
    for sound in SOUNDS.get(language, ()):
        for m in re.finditer(re.escape(sound), text):
            if not any(taken[m.start():m.end()]):
                spans.append((sound, m.start(), m.end()))
                for i in range(m.start(), m.end()):
                    taken[i] = True
    return sorted(spans, key=lambda s: s[1])


def normalize_language(value: str | None, default: str = "en") -> str:
    key = (value or "").strip().lower()
    aliases = {"english": "en", "engels": "en", "dutch": "nl", "nederlands": "nl",
               "hindi": "hi", "hindustani": "hi", "urdu": "hi"}
    key = aliases.get(key, key)
    return key if key in LANGUAGES else default


def find_word(text: str) -> Word | None:
    """A word spoken in any of the three languages -> its entry."""
    t = " ".join((text or "").lower().split())
    t = re.sub(r"^(de|het|een|the|a|an)\s+", "", t)
    for w in WORDS:
        if t in (w.key, w.en.lower(), w.nl.lower(), w.hi, w.hi_roman.lower()):
            return w
    return None


class VocabularyTrainer:
    """Picks the next words to teach and reviews, per child and language."""

    def __init__(self, tracker):
        self.tracker = tracker

    def next_words(self, child, language: str, category: str | None = None, count: int = 3) -> list[Word]:
        seen = self.tracker.words_seen(child, language)
        pool = [w for w in WORDS if category in (None, w.category)]
        fresh = [w for w in pool if w.key not in seen]
        if category is None and fresh:
            # stay within one category per lesson - easier for a 4 year old
            first = fresh[0].category
            fresh = [w for w in fresh if w.category == first]
        return (fresh or pool)[:count]

    def review_words(self, child, language: str, count: int = 3) -> list[Word]:
        keys = self.tracker.words_to_review(child, language, count)
        return [WORDS_BY_KEY[k] for k in keys if k in WORDS_BY_KEY]

    @staticmethod
    def translate(text: str) -> Word | None:
        return find_word(text)
