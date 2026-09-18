"""ChildLearningMode: short, playful practice for 4-6 year olds.

Quizzes are three-option, answerable by voice ("baa", "number two", "the
second one"), always followed by praise or gentle encouragement - never
"wrong". Every answer feeds the weak-areas tracker so revision targets what
the child actually finds hard.
"""
from __future__ import annotations

import dataclasses
import difflib
import random
import re

from .curriculum import (
    ALPHABET, DUAS, ENCOURAGEMENT, MANNERS, POSITIVE_FEEDBACK, alphabet_lessons,
)
from .recitation import normalize
from .tracker import LearningTracker, Student

_ORDINALS = {"one": 0, "first": 0, "1": 0, "ek": 0, "pehla": 0,
             "two": 1, "second": 1, "2": 1, "do": 1, "doosra": 1, "dusra": 1,
             "three": 2, "third": 2, "3": 2, "teen": 2, "teesra": 2}


@dataclasses.dataclass
class Quiz:
    kind: str
    question: str
    options: list[str]
    answer: int
    area: str                      # weak-area key if missed
    lesson_key: str | None = None
    extra: dict = dataclasses.field(default_factory=dict)

    def display(self) -> dict:
        return {"mode": "quiz", "title": "Quiz time!", "question": self.question,
                "options": [{"n": i + 1, "text": o} for i, o in enumerate(self.options)]}

    def spoken(self) -> str:
        opts = "; ".join(f"number {i + 1}: {o}" for i, o in enumerate(self.options))
        return f"{self.question} {opts}."


def _norm_latin(text: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", (text or "").lower()).strip()


class ChildLearningMode:
    def __init__(self, tracker: LearningTracker, rng: random.Random | None = None):
        self.tracker = tracker
        self.rng = rng or random.Random()
        self._active: dict[int, Quiz] = {}

    def praise(self) -> str:
        return self.rng.choice(POSITIVE_FEEDBACK)

    def encourage(self) -> str:
        return self.rng.choice(ENCOURAGEMENT)

    def active_quiz(self, student: Student) -> Quiz | None:
        return self._active.get(student.id)

    # -- making questions ----------------------------------------------------

    def new_quiz(self, student: Student, kind: str | None = None, ayahs=None) -> Quiz:
        """kind: letter_name, letter_find, next_word, dua, manners - or pick
        one that suits what the child has been learning."""
        kinds = ["letter_name", "letter_find", "dua", "manners"]
        if ayahs and sum(len(normalize(a.arabic).split()) for a in ayahs) >= 3:
            kinds.append("next_word")
        weak = [w["area"].split(":", 1)[0] for w in self.tracker.weak_areas(student)]
        kind = kind if kind in kinds else (
            "letter_name" if "letter" in weak else self.rng.choice(kinds))
        quiz = getattr(self, f"_{kind}")(student, ayahs)
        self._active[student.id] = quiz
        return quiz

    def _known_letters(self, student: Student):
        cursor = self.tracker.cursor(student, "alphabet")
        lessons = alphabet_lessons(2 if student.is_child else 4)
        upto = min(cursor.get("index", 0), len(lessons) - 1)
        known = [l for lesson in lessons[:upto + 1] for l in lesson]
        return known if len(known) >= 3 else list(ALPHABET[:4])

    def _options(self, correct, pool, key=lambda x: x):
        others = [p for p in pool if key(p) != key(correct)]
        picks = self.rng.sample(others, min(2, len(others))) + [correct]
        self.rng.shuffle(picks)
        return picks, picks.index(correct)

    def _letter_name(self, student, _ayahs):
        known = self._known_letters(student)
        # Weak letters come up more often.
        weak = {w["area"].split(":", 1)[1] for w in self.tracker.weak_areas(student, 10)
                if w["area"].startswith("letter:")}
        pool = [l for l in known if l.char in weak] or known
        letter = self.rng.choice(pool)
        picks, answer = self._options(letter, list(ALPHABET), key=lambda l: l.char)
        return Quiz("letter_name", f"What is this letter: {letter.char}?",
                    [p.name for p in picks], answer, f"letter:{letter.char}",
                    extra={"char": letter.char})

    def _letter_find(self, student, _ayahs):
        letter = self.rng.choice(self._known_letters(student))
        picks, answer = self._options(letter, list(ALPHABET), key=lambda l: l.char)
        return Quiz("letter_find", f"Which one is {letter.name}?",
                    [p.char for p in picks], answer, f"letter:{letter.char}",
                    extra={"names": [p.name for p in picks]})

    def _next_word(self, student, ayahs):
        flat = [w for a in ayahs for w in a.arabic.split() if normalize(w)]
        i = self.rng.randrange(1, len(flat))
        prompt = " ".join(flat[max(0, i - 3):i])
        correct = flat[i]
        pool = list(dict.fromkeys(flat))
        picks, answer = self._options(correct, pool, key=normalize)
        return Quiz("next_word", f"What comes next after: {prompt} ...?", picks, answer,
                    f"word:{ayahs[0].surah}:{normalize(correct)}")

    def _dua(self, student, _ayahs):
        phrases = [d for d in DUAS if d.phrase_translit]
        dua = self.rng.choice(phrases)
        picks, answer = self._options(dua, phrases, key=lambda d: d.key)
        return Quiz("dua", f"What do we say {dua.when.lower()}?",
                    [p.phrase_translit for p in picks], answer, f"dua:{dua.key}")

    def _manners(self, student, _ayahs):
        m = self.rng.choice(MANNERS)
        picks, answer = self._options(m, list(MANNERS), key=lambda x: x.key)
        return Quiz("manners", "Which one is a good manner we learned?",
                    [p.title for p in picks], answer, f"manners:{m.key}")

    # -- checking answers ----------------------------------------------------

    def match_answer(self, quiz: Quiz, spoken: str) -> int | None:
        """Which option the child meant: by number, or by (fuzzy) text."""
        said = _norm_latin(spoken)
        for word in said.split():
            if word in _ORDINALS and _ORDINALS[word] < len(quiz.options):
                return _ORDINALS[word]
        candidates = list(quiz.options)
        if quiz.kind == "letter_find":
            candidates = quiz.extra["names"]
            for i, opt in enumerate(quiz.options):          # Arabic letter spoken/typed
                if opt in spoken:
                    return i
        if quiz.kind == "next_word":
            said_ar = normalize(spoken)
            scores = [difflib.SequenceMatcher(None, said_ar, normalize(o)).ratio() for o in candidates]
        else:
            scores = [difflib.SequenceMatcher(None, said, _norm_latin(o)).ratio() for o in candidates]
        best = max(range(len(scores)), key=scores.__getitem__)
        return best if scores[best] >= 0.6 else None

    def answer(self, student: Student, spoken: str) -> dict:
        quiz = self._active.get(student.id)
        if quiz is None:
            return {"ok": False, "message": "There's no quiz running - say 'start a quiz' first."}
        choice = self.match_answer(quiz, spoken)
        if choice is None:
            return {"ok": True, "understood": False,
                    "message": f"I didn't catch that. {quiz.spoken()}"}
        correct = choice == quiz.answer
        self.tracker.note_area(student, quiz.area, correct)
        self.tracker.log(student, "quiz", f"{quiz.kind}: {quiz.question}", 1.0 if correct else 0.0)
        del self._active[student.id]
        right = quiz.options[quiz.answer]
        if correct:
            msg = f"{self.praise()} Yes, it's {right}!"
        else:
            msg = f"{self.encourage()} The answer is {right}."
            if quiz.kind == "letter_name":
                letter = next(l for l in ALPHABET if l.char == quiz.extra["char"])
                msg += f" {letter.char} is {letter.name} - {letter.sound}."
        return {"ok": True, "understood": True, "correct": correct, "answer": right,
                "message": msg, "display": {"mode": "feedback", "correct": correct,
                                            "title": "MashaAllah!" if correct else "Good try!",
                                            "text": msg}}

    def today_score(self, student: Student) -> tuple[int, int]:
        quizzes = [a for a in self.tracker.activity(student) if a["kind"] == "quiz"]
        return sum(1 for q in quizzes if q["score"]), len(quizzes)
