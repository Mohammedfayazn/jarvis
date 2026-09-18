"""The teaching curriculum: what is taught, in what order.

Rule for this file: no Quran text and no hadith text. Lessons hold
*references* (surah:ayah, collection + number) and the text is always
fetched from the verified sources at lesson time. What IS written here is
teaching material - letter names and sound hints, plain-English
explanations of tajweed rules, child-friendly summaries of manners - and it
is presented as the lesson's explanation, never as scripture.

Every hadith reference below was checked against the source it is fetched
from (tests/test_islamic.py re-verifies them), because hadith numbering
differs between editions and a wrong number would quote the wrong hadith.
"""
from __future__ import annotations

import dataclasses


@dataclasses.dataclass(frozen=True)
class Letter:
    char: str
    name: str
    sound: str          # how to say it, for an English/Urdu speaker
    group: str          # letters taught together


# The 28 letters in traditional order. Sound hints describe articulation
# in everyday terms; they are a teaching aid, not a tajweed text.
ALPHABET: tuple[Letter, ...] = (
    Letter("ا", "Alif", "a long 'aa' - carries the vowel, like 'a' in 'father'", "alif"),
    Letter("ب", "Baa", "'b' as in 'ball' - one dot below", "baa"),
    Letter("ت", "Taa", "light 't' as in 'tree' - two dots above", "baa"),
    Letter("ث", "Thaa", "'th' as in 'think' - three dots above, tongue between the teeth", "baa"),
    Letter("ج", "Jeem", "'j' as in 'jam' - one dot inside", "jeem"),
    Letter("ح", "Haa", "a breathy 'h' from the middle of the throat - like breathing on glasses", "jeem"),
    Letter("خ", "Khaa", "'kh' like 'ch' in Scottish 'loch' - dot on top", "jeem"),
    Letter("د", "Daal", "'d' as in 'door'", "daal"),
    Letter("ذ", "Dhaal", "'th' as in 'this' - one dot above", "daal"),
    Letter("ر", "Raa", "a rolled 'r'", "raa"),
    Letter("ز", "Zaay", "'z' as in 'zoo' - one dot above", "raa"),
    Letter("س", "Seen", "'s' as in 'sun'", "seen"),
    Letter("ش", "Sheen", "'sh' as in 'ship' - three dots above", "seen"),
    Letter("ص", "Saad", "a heavy 's' - mouth full, tongue low", "saad"),
    Letter("ض", "Daad", "a heavy 'd' from the sides of the tongue", "saad"),
    Letter("ط", "Taa (heavy)", "a heavy 't' - mouth full", "taa"),
    Letter("ظ", "Dhaa (heavy)", "a heavy 'th' as in 'this'", "taa"),
    Letter("ع", "Ayn", "a squeezed sound from the middle of the throat - no English letter like it", "ayn"),
    Letter("غ", "Ghayn", "a gargling 'gh', like a French 'r'", "ayn"),
    Letter("ف", "Faa", "'f' as in 'fish' - one dot above", "faa"),
    Letter("ق", "Qaaf", "a deep 'q' from the back of the throat - two dots above", "faa"),
    Letter("ك", "Kaaf", "'k' as in 'kite'", "kaaf"),
    Letter("ل", "Laam", "'l' as in 'lamp'", "kaaf"),
    Letter("م", "Meem", "'m' as in 'moon'", "meem"),
    Letter("ن", "Noon", "'n' as in 'nose' - one dot above", "meem"),
    Letter("ه", "Haa (light)", "a soft 'h' from the chest, as in 'hat'", "haa"),
    Letter("و", "Waaw", "'w' as in 'water', or a long 'oo'", "haa"),
    Letter("ي", "Yaa", "'y' as in 'yes', or a long 'ee' - two dots below", "haa"),
)
LETTERS_BY_CHAR = {letter.char: letter for letter in ALPHABET}

# Pairs learners (and speech recognisers) mix up most, with how to tell
# them apart. Used for pronunciation feedback.
CONFUSABLE = {
    frozenset("حه"): "ح comes from the middle of the throat (breathy); ه is a soft h from the chest.",
    frozenset("عا"): "ع is squeezed from the middle of the throat; ا is just the vowel.",
    frozenset("عء"): "ع is squeezed from the throat; ء (hamza) is a short catch in the voice.",
    frozenset("قك"): "ق is deep in the throat; ك is further forward, like English k.",
    frozenset("صس"): "ص is heavy with the mouth full; س is light.",
    frozenset("طت"): "ط is heavy with the mouth full; ت is light.",
    frozenset("ضد"): "ض is heavy, from the sides of the tongue; د is light.",
    frozenset("ظز"): "ظ is a heavy 'th' with the tongue out; ز is a z.",
    frozenset("ذز"): "ذ is 'th' as in 'this' with the tongue out; ز is a z.",
    frozenset("ثس"): "ث is 'th' as in 'think' with the tongue out; س is an s.",
    frozenset("خح"): "خ is a rough 'kh'; ح is a breathy h without any roughness.",
    frozenset("غخ"): "غ is voiced (gargled); خ is voiceless (like loch).",
}


def confusion_hint(expected: str, said: str) -> str | None:
    return CONFUSABLE.get(frozenset(expected + said))


def alphabet_lessons(per_lesson: int) -> list[tuple[Letter, ...]]:
    return [ALPHABET[i:i + per_lesson] for i in range(0, len(ALPHABET), per_lesson)]


@dataclasses.dataclass(frozen=True)
class TajweedLesson:
    key: str
    title: str
    explanation: str
    examples: tuple[tuple[int, int, str], ...]   # (surah, ayah, where to listen)


# Basic tajweed for beginners - standard rules of Hafs 'an 'Asim. Examples
# point to ayahs in short surahs; the text shown is fetched, and the note
# says which word to listen for.
TAJWEED: tuple[TajweedLesson, ...] = (
    TajweedLesson("harakat", "Short vowels (harakat)",
                  "Fatha (a line above) says 'a', kasra (a line below) says 'i', damma (a small "
                  "waw above) says 'u'. Sukun (a small circle) means no vowel. Tanween (a double "
                  "mark) adds an 'n' sound: an, in, un.",
                  ((112, 1, "qul - the sukun on laam; ahadun - tanween at the end"),)),
    TajweedLesson("madd", "Natural elongation (madd tabi'i)",
                  "When alif, waaw or yaa follows its matching vowel, the vowel is stretched for "
                  "two counts: 'maa', 'moo', 'mee'.",
                  ((1, 4, "maaliki - the 'maa' is two counts"), (114, 1, "an-naas - the 'naa'"))),
    TajweedLesson("qalqalah", "Echo letters (qalqalah)",
                  "The five letters ق ط ب ج د (remembered as 'qutbu jad') get a small bounce or "
                  "echo when they have a sukun - most clearly when you stop on them.",
                  ((112, 1, "ahad - bounce on the final daal when stopping"),
                   (113, 1, "al-falaq - bounce on the final qaaf"))),
    TajweedLesson("ghunnah", "Nasal sound (ghunnah)",
                  "A noon or meem with a shaddah is held in the nose for two counts.",
                  ((114, 1, "an-naas - the doubled noon"), (108, 1, "innaa - the doubled noon"))),
    TajweedLesson("lam-shamsiyyah", "Sun and moon letters",
                  "In 'al-', the laam is silent before 'sun letters' and the next letter is "
                  "doubled (an-naas); it is pronounced clearly before 'moon letters' (al-falaq).",
                  ((114, 1, "an-naas - silent laam (sun letter)"),
                   (113, 1, "al-falaq - clear laam (moon letter)"))),
    TajweedLesson("lafz-allah", "The laam in 'Allah'",
                  "The laam in the name Allah is heavy (full) after a fatha or damma, and light "
                  "after a kasra.",
                  ((112, 1, "huwa-llahu - heavy, after the fatha of 'huwa'"),
                   (1, 1, "bismi-llahi - light, after a kasra"))),
    TajweedLesson("izhar", "Clear noon (izhar)",
                  "A noon with sukun or tanween is said clearly, with no nasal hold, before the "
                  "six throat letters ء ه ع ح غ خ.",
                  ((112, 4, "kufuwan ahad - tanween before a hamza, said clearly"),)),
    TajweedLesson("idgham", "Merging (idgham)",
                  "A noon with sukun or tanween merges into the next word when it starts with one "
                  "of ي ر م ل و ن ('yarmaloon'). With ي ن م و the merge keeps a nasal hold.",
                  ((111, 1, "lahabin-wa tabb - the tanween merges into the waaw"),)),
    TajweedLesson("iqlab", "Changing to meem (iqlab)",
                  "A noon with sukun or tanween before ب turns into a hidden meem sound, held in "
                  "the nose.",
                  ((104, 4, "layumbadhanna - the noon sounds like meem before the baa"),)),
    TajweedLesson("ikhfa", "Hiding (ikhfa)",
                  "Before the remaining fifteen letters, a noon with sukun or tanween is "
                  "half-hidden: said in the nose, between clear and merged.",
                  ((113, 2, "min sharri - the noon is hidden before the sheen"),)),
    TajweedLesson("meem-sakinah", "Meem with sukun",
                  "A meem with sukun is hidden with a nasal sound before ب (ikhfa shafawi), "
                  "merged before another م, and clear before every other letter.",
                  ((105, 4, "tarmeehim bi-hijaratin - meem hidden before baa"),)),
)
TAJWEED_BY_KEY = {lesson.key: lesson for lesson in TAJWEED}

# Short surahs in the order children usually memorise them (end of Juz 'Amma).
MEMORIZATION_ORDER = (1, 112, 113, 114, 108, 103, 110, 109, 111, 107, 106, 105,
                      104, 102, 101, 100, 99, 97, 95, 94, 93)


@dataclasses.dataclass(frozen=True)
class Dua:
    key: str
    title: str
    when: str
    # Either a Quran reference (text fetched), or a short phrase whose
    # wording is fixed and cited to a verified hadith.
    quran: tuple[int, int] | None = None
    phrase_ar: str | None = None
    phrase_translit: str | None = None
    meaning: str | None = None
    hadith: tuple[str, int] | None = None


DUAS: tuple[Dua, ...] = (
    Dua("bismillah", "Bismillah before eating", "Before you start eating",
        phrase_ar="بِسْمِ اللَّهِ", phrase_translit="Bismillah", meaning="In the name of Allah",
        hadith=("bukhari", 5376)),
    Dua("alhamdulillah-sneeze", "When you sneeze", "After sneezing - and the reply",
        phrase_ar="الْحَمْدُ لِلَّهِ", phrase_translit="Alhamdulillah",
        meaning="All praise is for Allah. The listener replies 'Yarhamukallah' - "
                "may Allah have mercy on you.",
        hadith=("bukhari", 6224)),
    Dua("salam", "Greeting with salaam", "When you meet someone",
        phrase_ar="السَّلَامُ عَلَيْكُمْ", phrase_translit="As-salaamu 'alaykum",
        meaning="Peace be upon you - and we answer a greeting with one as good or better",
        quran=(4, 86)),
    Dua("knowledge", "For knowledge", "Before studying", quran=(20, 114)),
    Dua("parents", "For your parents", "Any time, especially after prayer", quran=(17, 24)),
    Dua("good-both-worlds", "Good in this life and the next", "Any time", quran=(2, 201)),
    Dua("family", "For your family", "Any time", quran=(25, 74)),
)


@dataclasses.dataclass(frozen=True)
class MannersLesson:
    key: str
    title: str
    child_summary: str     # what a 5-year-old is told - explanation, not quotation
    adult_note: str
    source_hadith: tuple[str, int] | None = None
    source_quran: tuple[int, int] | None = None
    # A word from the verified text, checked by the tests so a wrong
    # reference can never slip in unnoticed.
    must_contain: str = ""


MANNERS: tuple[MannersLesson, ...] = (
    MannersLesson("eating", "Eating nicely",
                  "Say Bismillah, eat with your right hand, and eat from what is in front of you.",
                  "Three table manners taught to a boy in the Prophet's care.",
                  source_hadith=("bukhari", 5376), must_contain="right hand"),
    MannersLesson("smile", "Smiling is charity",
                  "When you smile at someone, Allah counts it as a good deed, like giving charity.",
                  "Smiling, helping someone find their way, and moving harm from the road are all charity.",
                  source_hadith=("tirmidhi", 1956), must_contain="smil"),
    MannersLesson("truth", "Always tell the truth",
                  "Telling the truth leads to good things and to Jannah. Lying leads to bad things.",
                  "Truthfulness leads to righteousness; falsehood leads to wickedness.",
                  source_hadith=("bukhari", 6094), must_contain="Truthfulness"),
    MannersLesson("share", "Wanting good for others",
                  "Wish for your friends and family the same good things you want for yourself.",
                  "Faith is not complete until one wishes for one's brother what one wishes for oneself.",
                  source_hadith=("bukhari", 13), must_contain="likes for himself"),
    MannersLesson("parents", "Being kind to parents",
                  "Be gentle with Ammi and Abbu - never say even 'uff' to them.",
                  "Allah joins worshipping Him with kindness to parents; not even 'uff'.",
                  source_quran=(17, 23), must_contain="parents"),
    MannersLesson("mother", "Your mother first",
                  "The Prophet said the person who most deserves your kindness is your mother - "
                  "he said it three times - and then your father.",
                  "Asked who deserves the best companionship: your mother (three times), then your father.",
                  source_hadith=("bukhari", 5971), must_contain="Your mother"),
    MannersLesson("good-words", "Say good words or stay quiet",
                  "If you can't say something kind, it is better to stay quiet. And be kind to "
                  "neighbours and guests.",
                  "Belief in Allah and the Last Day shows in not hurting neighbours, honouring "
                  "guests, and speaking good or staying silent.",
                  source_hadith=("bukhari", 6136), must_contain="neighbor"),
    MannersLesson("mercy", "Be kind and merciful",
                  "Be kind to people and animals - Allah is kind to those who are kind.",
                  "The merciful are shown mercy by Ar-Rahman.",
                  source_hadith=("tirmidhi", 1924), must_contain="merciful"),
    MannersLesson("sneeze", "When someone sneezes",
                  "When you sneeze say 'Alhamdulillah'. When someone else sneezes and says it, "
                  "say 'Yarhamukallah'.",
                  "The etiquette of sneezing and replying.",
                  source_hadith=("bukhari", 6224), must_contain="sneezes"),
)

POSITIVE_FEEDBACK = (
    "MashaAllah, well done!", "Excellent, you're a star!", "Great job!",
    "SubhanAllah, that was lovely!", "Wonderful, keep going!", "Yes! You got it!",
    "Beautiful! Allah loves those who learn His book.",
)
ENCOURAGEMENT = (
    "Good try! Let's do it together.", "Almost! Listen once more.",
    "That's okay - practice makes it easy. Try again!", "Nice effort! One more time.",
)
