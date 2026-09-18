"""ConversationEngine: starters, follow-ups, and a light look at what the
child said.

Jarvis's language model holds the actual conversation (and writes the
"bigger sentence"); this module gives it age-appropriate questions in the
right language and measures each utterance: how many words, which
languages were mixed, and whether it's a short "telegraphic" sentence that
is ready to be expanded ("dog running" -> "The dog is running").

These are practice measurements, not an assessment - no norms, no scores
against other children.
"""
from __future__ import annotations

import dataclasses
import random
import re

STARTERS = {
    "day": {"en": "What did you do today?", "nl": "Wat heb je vandaag gedaan?",
            "hi": "आज तुमने क्या किया?"},
    "food": {"en": "What did you eat today?", "nl": "Wat heb je vandaag gegeten?",
             "hi": "आज तुमने क्या खाया?"},
    "games": {"en": "What games did you play?", "nl": "Welke spelletjes heb je gespeeld?",
              "hi": "तुमने कौन से खेल खेले?"},
    "friends": {"en": "Who did you play with?", "nl": "Met wie heb je gespeeld?",
                "hi": "तुम किसके साथ खेलीं?"},
    "animals": {"en": "What is your favourite animal? Why?", "nl": "Wat is je lievelingsdier? Waarom?",
                "hi": "तुम्हारा पसंदीदा जानवर कौन सा है? क्यों?"},
    "school": {"en": "What did you do at school?", "nl": "Wat heb je op school gedaan?",
               "hi": "स्कूल में तुमने क्या किया?"},
}
FOLLOW_UPS = {
    "en": ("And then what happened?", "Tell me more!", "What colour was it?",
           "How did you feel?", "Who else was there?"),
    "nl": ("En toen?", "Vertel eens meer!", "Welke kleur was het?",
           "Hoe voelde je je?", "Wie was er nog meer?"),
    "hi": ("फिर क्या हुआ?", "और बताओ!", "वो किस रंग का था?", "तुम्हें कैसा लगा?",
           "और कौन था वहाँ?"),
}
PRAISE = {
    "en": ("Very good!", "Great talking!", "I love that!", "Wow, nice!"),
    "nl": ("Heel goed!", "Goed zo!", "Wat leuk!", "Super!"),
    "hi": ("बहुत बढ़िया!", "शाबाश!", "वाह, बहुत अच्छा!"),
}
EXPAND_INVITE = {
    "en": "Can we make the sentence a little bigger?",
    "nl": "Kunnen we de zin een beetje groter maken?",
    "hi": "क्या हम वाक्य थोड़ा बड़ा बना सकते हैं?",
}

# Small lexicons for spotting which language a word belongs to. Words that
# are the same in English and Dutch ("is", "in", "water", "mama") count as
# neither, so they never make a sentence look "mixed".
_EN = set("""the a an i you he she it we they my your his her our is am are was were be
and but or not no yes to of on at with for from this that what where who why how
dog cat play played playing eat ate eating go went going have has had like want
big small little happy sad red blue green yellow run running jump jumped look see saw
today yesterday school friend friends mum dad please thank car ball house""".split())
_NL = set("""de het een ik jij je hij zij wij jullie mijn jouw zijn haar ons is ben bent
was waren en maar of niet nee ja naar van op met voor uit dit dat wat waar wie
waarom hoe hond kat spelen gespeeld speel eten gegeten ga gaan ging heb hebt heeft had
wil groot klein blij verdrietig rood blauw groen geel rennen rent springen kijk zie zag
vandaag gisteren school vriendje juf mama papa alsjeblieft dank huis auto bal ook dan
toen leuk mooi lief""".split())
_HI_ROMAN = set("""hai hain tha thi nahi nahin haan kya kaun kahan kyun kaise mera meri mere
tera teri tum main mein hum aur bhi ko ka ki ke se par ghar khana paani accha acha
chalo dekho bahut bohot abhi kal aaj""".split())
_BOTH = _EN & _NL
_DEVANAGARI = re.compile(r"[ऀ-ॿ]")
_VERBISH_EN = re.compile(r"(ing|ed)$")
_AUX = {"en": {"is", "am", "are", "was", "were", "has", "have", "can", "will", "do", "does", "did"},
        "nl": {"is", "ben", "bent", "zijn", "was", "waren", "heb", "hebt", "heeft", "kan", "wil", "gaat", "ga"}}
_ARTICLES = {"en": {"the", "a", "an", "my", "your"}, "nl": {"de", "het", "een", "mijn", "jouw"}}


@dataclasses.dataclass(frozen=True)
class UtteranceInfo:
    text: str
    words: int
    languages: list[str]          # detected, in order of frequency
    mixed: bool
    short: bool                   # 1-3 words: a good one to expand
    hints: list[str]              # what a bigger sentence could add

    def as_dict(self) -> dict:
        return dataclasses.asdict(self)


def tokenize(text: str) -> list[str]:
    return re.findall(r"[\wऀ-ॿ']+", (text or "").lower())


def detect_languages(tokens: list[str]) -> list[str]:
    counts = {"en": 0, "nl": 0, "hi": 0}
    for t in tokens:
        if _DEVANAGARI.search(t) or t in _HI_ROMAN:
            counts["hi"] += 1
        elif t in _BOTH:
            continue
        elif t in _EN:
            counts["en"] += 1
        elif t in _NL:
            counts["nl"] += 1
    return [lang for lang, n in sorted(counts.items(), key=lambda kv: -kv[1]) if n]


def analyze_utterance(text: str, language: str = "en") -> UtteranceInfo:
    tokens = tokenize(text)
    langs = detect_languages(tokens)
    hints = []
    short = 0 < len(tokens) <= 3
    if short and language in ("en", "nl"):
        if not set(tokens) & _ARTICLES[language]:
            hints.append("add a word like " + ("'the'" if language == "en" else "'de' / 'het' / 'een'"))
        if not set(tokens) & _AUX[language]:
            if language == "en" and any(_VERBISH_EN.search(t) for t in tokens):
                hints.append("add 'is' before the -ing word")
            else:
                hints.append("add a doing word (verb)" if language == "en" else "voeg een werkwoord toe")
        hints.append("add where or what: 'in the park', 'with a ball'")
    return UtteranceInfo(text=text.strip(), words=len(tokens), languages=langs,
                         mixed=len(langs) >= 2, short=short, hints=hints)


class ConversationEngine:
    def __init__(self, rng: random.Random | None = None):
        self.rng = rng or random.Random()

    def starter(self, language: str, topic: str | None = None, avoid: set[str] | None = None) -> tuple[str, str]:
        topics = [t for t in STARTERS if t not in (avoid or set())] or list(STARTERS)
        topic = topic if topic in STARTERS else self.rng.choice(topics)
        return topic, STARTERS[topic][language]

    def follow_up(self, language: str) -> str:
        return self.rng.choice(FOLLOW_UPS[language])

    def praise(self, language: str) -> str:
        return self.rng.choice(PRAISE[language])

    def expansion_message(self, language: str, bigger: str) -> str:
        """'Very good! Can we make the sentence a little bigger? The dog is running.'"""
        return f"{self.praise(language)} {EXPAND_INVITE[language]} {bigger}"
