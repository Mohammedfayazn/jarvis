"""IslamicKnowledgeBase: sourced answers, with the categories kept apart.

Jarvis's language model writes the spoken answer; this layer decides what
it is allowed to quote. It returns only verified text, always labelled:

    QURAN    - Tanzil Uthmani text + Sahih International translation, with ref
    HADITH   - text from the collection, with reference and grading
    LESSON   - this tutor's own explanation (never presented as scripture)

and ANSWER_POLICY (added to Jarvis's system prompt) tells the model how to
add scholarly interpretation and differing opinions without blurring them
into the sources, and to say "I'm not certain" rather than guess.
"""
from __future__ import annotations

import re

from .curriculum import DUAS, MANNERS
from .sources import HadithSource, QuranSource, SourceUnavailable

ANSWER_POLICY = """\
Islamic questions - how to answer:
- Quote the Quran or a hadith ONLY from text an Islamic tool returned in
  this conversation. Never recite, write or paraphrase-as-quote Quran or
  hadith from your own memory - not even a short verse. If you need a verse
  or hadith you don't have, call islamic_sources first.
- Keep the kinds of knowledge visibly separate, and say which is which:
  "The Quran says (surah:ayah) ...", "In Sahih al-Bukhari (number) the
  Prophet (peace be upon him) said ...", "Scholars explain this as ...",
  "On this there are different valid opinions: the Hanafi view is ...,
  others hold ...".
- Give the hadith grading the tool returned. Never call a hadith authentic
  when the result doesn't say so.
- If scholars differ, say so and present the main views fairly, without
  picking one as the only truth.
- If you are not sure, say "I'm not certain" and point to a qualified
  scholar - never state uncertain information as fact. Personal rulings
  (fatwa: divorce, inheritance shares, whether a specific act is valid)
  belong with a local scholar; give general information only.
- With children: simple, warm, true. No frightening detail."""


def _label_ayahs(ayahs) -> str:
    lines = []
    for a in ayahs:
        lines.append(f"QURAN {a.ref}: {a.arabic}\n  Translation (Sahih International): {a.translation}")
    return "\n".join(lines)


def _label_hadith(h) -> str:
    return (f"HADITH - {h.reference} [{h.grading}]:\n  {h.text_en}"
            + (f"\n  Arabic: {h.text_ar}" if h.text_ar else ""))


class IslamicKnowledgeBase:
    def __init__(self, quran: QuranSource, hadith: HadithSource):
        self.quran = quran
        self.hadith_source = hadith

    def verses(self, surah: int, ayah: int = 1, to_ayah: int | None = None) -> dict:
        try:
            info = self.quran.surah(surah)
            ayahs = self.quran.ayahs(surah, ayah, to_ayah or ayah)
        except (ValueError, SourceUnavailable) as exc:
            return {"ok": False, "message": f"I couldn't get that from the verified Quran source: {exc}"}
        return {"ok": True, "surah": info.as_dict(), "ayahs": [a.as_dict() for a in ayahs],
                "message": f"Surah {info.name_en} ({info.meaning}):\n" + _label_ayahs(ayahs)}

    def search_quran(self, query: str) -> dict:
        try:
            ayahs = self.quran.search(query, limit=6)
        except SourceUnavailable as exc:
            return {"ok": False, "message": f"The Quran search is unavailable: {exc}"}
        if not ayahs:
            return {"ok": True, "found": False, "message":
                    f"No ayah's translation contains '{query}'. Try a simpler English word "
                    "(e.g. 'parents', 'patience', 'charity')."}
        return {"ok": True, "found": True, "ayahs": [a.as_dict() for a in ayahs],
                "message": f"Ayahs whose translation mentions '{query}':\n" + _label_ayahs(ayahs)}

    def hadith(self, collection: str, number: int) -> dict:
        try:
            h = self.hadith_source.get(collection, number)
        except (ValueError, SourceUnavailable) as exc:
            return {"ok": False, "message": f"I couldn't verify that hadith: {exc}"}
        return {"ok": True, "hadith": h.as_dict(), "message": _label_hadith(h)}

    def topic(self, query: str) -> dict:
        """Curated, pre-verified sources for everyday topics (manners, duas)."""
        words = set(re.findall(r"[a-z]+", (query or "").lower())) - {"the", "a", "in", "of", "about", "islam"}
        if not words:
            return {"ok": False, "message": "What topic?"}
        hits = []
        for m in MANNERS:
            text = f"{m.key} {m.title} {m.child_summary} {m.adult_note}".lower()
            if any(w in text for w in words):
                hits.append(("manners", m))
        for d in DUAS:
            text = f"{d.key} {d.title} {d.when} {d.meaning or ''}".lower()
            if any(w in text for w in words):
                hits.append(("dua", d))
        if not hits:
            return {"ok": True, "found": False, "message":
                    f"I have no pre-verified sources on '{query}'. Search the Quran by keyword, "
                    "or look up a specific hadith by collection and number."}
        parts, sources = [], []
        for kind, item in hits[:3]:
            try:
                if kind == "manners":
                    src = (self.hadith_source.get(*item.source_hadith) if item.source_hadith
                           else self.quran.ayah(*item.source_quran))
                    label = _label_hadith(src) if item.source_hadith else _label_ayahs([src])
                    parts.append(f"LESSON - {item.title}: {item.adult_note}\n{label}")
                else:
                    if item.quran:
                        label = _label_ayahs([self.quran.ayah(*item.quran)])
                    else:
                        label = _label_hadith(self.hadith_source.get(*item.hadith))
                    phrase = (f" Words: {item.phrase_ar} ({item.phrase_translit}) - {item.meaning}."
                              if item.phrase_ar else "")
                    parts.append(f"DUA - {item.title} ({item.when}).{phrase}\n{label}")
                sources.append(item.key)
            except SourceUnavailable as exc:
                parts.append(f"(Source for '{item.title}' unavailable right now: {exc})")
        return {"ok": True, "found": True, "topics": sources, "message": "\n\n".join(parts)}
