"""QuranTeacher: builds lessons - alphabet, tajweed, verse-by-verse
memorisation, duas and manners - sized for the learner (a 4-6 year old gets
two letters or one short ayah; a parent gets more).

A Lesson carries three things: what Jarvis should say, what the HUD should
show, and which recitation audio to play. Scripture inside it always comes
from the verified sources.
"""
from __future__ import annotations

import dataclasses

from .curriculum import (
    DUAS, MANNERS, MEMORIZATION_ORDER, TAJWEED, alphabet_lessons,
)
from .sources import HadithSource, QuranSource
from .tracker import LearningTracker, Student

TRACKS = ("alphabet", "tajweed", "surah", "dua", "manners")


@dataclasses.dataclass
class Lesson:
    key: str                       # "alphabet:2", "surah:112:1-2"
    track: str
    title: str
    speech: str                    # what Jarvis says (the tool message)
    display: dict                  # HUD payload
    audio: list[int] = dataclasses.field(default_factory=list)   # global ayah numbers
    repeat: int = 1
    ayahs: list = dataclasses.field(default_factory=list)        # for a recitation test
    position: dict = dataclasses.field(default_factory=dict)     # cursor for this lesson
    finished_track: bool = False

    def as_dict(self) -> dict:
        return {"key": self.key, "track": self.track, "title": self.title,
                "audio_ayahs": self.audio, "repeat": self.repeat,
                "ayahs": [a.as_dict() for a in self.ayahs], "display": self.display}


def _verse_items(ayahs, focus: set[int] | None = None) -> list[dict]:
    return [{"ref": a.ref, "ayah": a.ayah, "arabic": a.arabic, "translit": a.transliteration,
             "translation": a.translation, "focus": focus is None or a.ayah in focus}
            for a in ayahs]


class QuranTeacher:
    def __init__(self, quran: QuranSource, hadith: HadithSource, tracker: LearningTracker):
        self.quran = quran
        self.hadith = hadith
        self.tracker = tracker

    # -- sizing ------------------------------------------------------------

    @staticmethod
    def letters_per_lesson(student: Student) -> int:
        return 2 if student.is_child else 4

    @staticmethod
    def memorization_words(student: Student) -> int:
        return 8 if student.is_child else 30

    # -- entry point -------------------------------------------------------

    def lesson(self, student: Student, track: str, mode: str = "continue",
               surah: int | None = None, ayah: int | None = None) -> Lesson:
        """mode: 'continue' (current lesson), 'next' (advance), 'repeat'."""
        track = track if track in TRACKS else "surah"
        builders = {"alphabet": self._alphabet, "tajweed": self._tajweed, "surah": self._surah,
                    "dua": self._dua, "manners": self._manners}
        cursor = self.tracker.cursor(student, track)
        if track == "surah" and surah:
            cursor = {"surah": int(surah), "ayah": int(ayah or 1)}
        elif mode == "next" and cursor:
            cursor = self._advance(student, track, cursor)
        lesson = builders[track](student, cursor)
        self.tracker.set_cursor(student, track, **lesson.position)
        self.tracker.record_lesson(student, lesson.key, completed=False)
        return lesson

    def _advance(self, student: Student, track: str, cursor: dict) -> dict:
        if track == "surah":
            s, a = cursor.get("surah", MEMORIZATION_ORDER[0]), cursor.get("ayah", 1)
            span = cursor.get("span", 1)
            info = self.quran.surah(s)
            if a + span <= info.ayah_count:
                return {"surah": s, "ayah": a + span}
            return {"surah": self._next_surah(student, s), "ayah": 1}
        return {"index": cursor.get("index", 0) + 1}

    def _next_surah(self, student: Student, current: int) -> int:
        order = list(MEMORIZATION_ORDER)
        lengths = {s.number: s.ayah_count for s in self.quran.surahs()}
        done = set(self.tracker.memorized_surahs(student, lengths))
        start = order.index(current) + 1 if current in order else 0
        for s in order[start:] + order[:start]:
            if s not in done and s != current:
                return s
        return current

    def mark_complete(self, student: Student, lesson_key: str, score: float | None = None) -> None:
        self.tracker.record_lesson(student, lesson_key, completed=True, score=score)

    # -- tracks ------------------------------------------------------------

    def _alphabet(self, student: Student, cursor: dict) -> Lesson:
        lessons = alphabet_lessons(self.letters_per_lesson(student))
        index = min(cursor.get("index", 0), len(lessons) - 1)
        letters = lessons[index]
        finished = cursor.get("index", 0) >= len(lessons)
        says = "; ".join(f"{l.char} is {l.name} - {l.sound}" for l in letters)
        speech = (f"Letters lesson {index + 1} of {len(lessons)}: {says}. "
                  "Say each one after me, then we'll do a little quiz.")
        return Lesson(
            key=f"alphabet:{index}", track="alphabet",
            title=f"Arabic letters - lesson {index + 1}", speech=speech,
            display={"mode": "letters", "title": f"Arabic letters · lesson {index + 1}/{len(lessons)}",
                     "items": [{"char": l.char, "name": l.name, "sound": l.sound} for l in letters]},
            position={"index": index}, finished_track=finished,
        )

    def _tajweed(self, student: Student, cursor: dict) -> Lesson:
        index = min(cursor.get("index", 0), len(TAJWEED) - 1)
        rule = TAJWEED[index]
        examples, notes, audio = [], [], []
        for surah, ayah, note in rule.examples:
            a = self.quran.ayah(surah, ayah)
            examples.append(a)
            notes.append(f"{a.ref}: {note}")
            audio.append(a.global_number)
        items = _verse_items(examples)
        for item, note in zip(items, notes):
            item["note"] = note.split(": ", 1)[1]
        speech = (f"Tajweed lesson {index + 1}: {rule.title}. {rule.explanation} "
                  f"Listen for it in: {'; '.join(notes)}.")
        return Lesson(
            key=f"tajweed:{rule.key}", track="tajweed", title=rule.title, speech=speech,
            display={"mode": "verses", "title": f"Tajweed · {rule.title}",
                     "subtitle": rule.explanation, "items": items},
            audio=audio, repeat=2, ayahs=examples, position={"index": index},
        )

    def _surah(self, student: Student, cursor: dict) -> Lesson:
        surah = cursor.get("surah") or MEMORIZATION_ORDER[0]
        info = self.quran.surah(surah)
        start = min(max(1, cursor.get("ayah", 1)), info.ayah_count)
        # Take ayahs until the word budget is used (always at least one).
        budget, end = self.memorization_words(student), start
        ayahs = self.quran.ayahs(surah, start, min(info.ayah_count, start + 5))
        used, chosen = 0, []
        for a in ayahs:
            count = len(a.arabic.split())
            if chosen and used + count > budget:
                break
            chosen.append(a)
            used += count
        end = chosen[-1].ayah
        for a in chosen:
            self.tracker.learning(student, surah, a.ayah)
        # Show what came before (already learned) dimmed, for context.
        before = self.quran.ayahs(surah, max(1, start - 2), start - 1) if start > 1 else []
        span = f"{start}" if start == end else f"{start}-{end}"
        repeat = 3 if student.is_child else 2
        speech = (f"Surah {info.name_en} ({info.meaning}), ayah {span} of {info.ayah_count}. "
                  f"Listen {repeat} times, then recite it back to me. "
                  + " ".join(f"Ayah {a.ayah} means: {a.translation}" for a in chosen))
        return Lesson(
            key=f"surah:{surah}:{span}", track="surah",
            title=f"{info.name_en} {span}", speech=speech,
            display={"mode": "verses", "title": f"{info.name_en} · {info.name_ar}",
                     "subtitle": f"Ayah {span} of {info.ayah_count} - {info.meaning}",
                     "items": _verse_items(before + chosen, {a.ayah for a in chosen})},
            audio=[a.global_number for a in chosen], repeat=repeat, ayahs=chosen,
            position={"surah": surah, "ayah": start, "span": len(chosen)},
        )

    def _dua(self, student: Student, cursor: dict) -> Lesson:
        index = cursor.get("index", 0) % len(DUAS)
        dua = DUAS[index]
        items, audio, ayahs, source = [], [], [], ""
        if dua.quran:
            a = self.quran.ayah(*dua.quran)
            items, audio, ayahs = _verse_items([a]), [a.global_number], [a]
            source = f"Quran {a.ref}"
            words = f"From the Quran, {a.ref}: {a.translation}"
        else:
            words = f"{dua.phrase_translit} - {dua.meaning}"
        if dua.phrase_ar:
            items.insert(0, {"ref": "", "arabic": dua.phrase_ar, "translit": dua.phrase_translit,
                             "translation": dua.meaning, "focus": True})
        if dua.hadith:
            h = self.hadith.get(*dua.hadith)
            source = f"{h.reference} ({h.grading.split(' (')[0]})"
        speech = f"Dua lesson: {dua.title} - {dua.when}. {words}. Source: {source}."
        return Lesson(
            key=f"dua:{dua.key}", track="dua", title=dua.title, speech=speech,
            display={"mode": "verses", "title": f"Dua · {dua.title}",
                     "subtitle": f"{dua.when} · {source}", "items": items},
            audio=audio, repeat=2, ayahs=ayahs, position={"index": index},
        )

    def _manners(self, student: Student, cursor: dict) -> Lesson:
        index = cursor.get("index", 0) % len(MANNERS)
        m = MANNERS[index]
        if m.source_hadith:
            h = self.hadith.get(*m.source_hadith)
            source, quote = f"{h.reference} ({h.grading.split(' (')[0]})", h.text_en
            items = [{"ref": h.reference, "arabic": "", "translit": "", "translation": quote, "focus": True}]
            audio = []
        else:
            a = self.quran.ayah(*m.source_quran)
            source, quote = f"Quran {a.ref}", a.translation
            items, audio = _verse_items([a]), [a.global_number]
        told = m.child_summary if student.is_child else m.adult_note
        speech = f"Manners lesson: {m.title}. {told} Source: {source}."
        return Lesson(
            key=f"manners:{m.key}", track="manners", title=m.title, speech=speech,
            display={"mode": "verses", "title": f"Manners · {m.title}", "subtitle": told, "items": items},
            audio=audio, repeat=1, position={"index": index},
        )
