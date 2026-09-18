"""LessonPlanner: today's plan for one learner.

Four parts, as a teacher would set them: a Quran lesson, a dua, a manners
lesson, and revision (ayahs due for review plus the child's weak areas).
The plan follows each student's own position, so parent and child get
different plans, and asking twice on the same day gives the same plan.
"""
from __future__ import annotations

import dataclasses

from .curriculum import DUAS, MANNERS, TAJWEED, alphabet_lessons
from .tracker import LearningTracker, Student


@dataclasses.dataclass(frozen=True)
class PlanItem:
    part: str          # quran | dua | manners | revision
    track: str
    title: str
    detail: str

    def as_dict(self) -> dict:
        return dataclasses.asdict(self)


class LessonPlanner:
    def __init__(self, tracker: LearningTracker, quran):
        self.tracker = tracker
        self.quran = quran

    def daily_plan(self, student: Student) -> list[PlanItem]:
        items = [self._quran_item(student)]
        dua = DUAS[self.tracker.cursor(student, "dua").get("index", 0) % len(DUAS)]
        items.append(PlanItem("dua", "dua", dua.title, dua.when))
        manners = MANNERS[self.tracker.cursor(student, "manners").get("index", 0) % len(MANNERS)]
        items.append(PlanItem("manners", "manners", manners.title,
                              manners.child_summary if student.is_child else manners.adult_note))
        items.extend(self._revision(student))
        return items

    def _quran_item(self, student: Student) -> PlanItem:
        per = 2 if student.is_child else 4
        lessons = alphabet_lessons(per)
        letters_at = self.tracker.cursor(student, "alphabet").get("index", 0)
        letters_done = len(self.tracker.completed_lessons(student, "alphabet")) >= len(lessons)
        surah_cur = self.tracker.cursor(student, "surah")
        # Children start with letters, alongside one short surah; adults
        # (who usually read already) work on tajweed and memorisation.
        if student.is_child and not letters_done and not surah_cur:
            chars = " ".join(l.char for l in lessons[min(letters_at, len(lessons) - 1)])
            return PlanItem("quran", "alphabet", f"Letters lesson {letters_at + 1}", chars)
        if not student.is_child and not surah_cur:
            tj = TAJWEED[self.tracker.cursor(student, "tajweed").get("index", 0) % len(TAJWEED)]
            return PlanItem("quran", "tajweed", f"Tajweed: {tj.title}", tj.explanation[:90])
        s = surah_cur.get("surah", 1)
        info = self.quran.surah(s)
        return PlanItem("quran", "surah", f"Memorise {info.name_en}",
                        f"from ayah {surah_cur.get('ayah', 1)} of {info.ayah_count}")

    def _revision(self, student: Student) -> list[PlanItem]:
        out = []
        due = self.tracker.due_for_review(student, 5)
        if due:
            refs = ", ".join(f"{d['surah']}:{d['ayah']}" for d in due)
            out.append(PlanItem("revision", "surah", "Recite from memory", refs))
        weak = self.tracker.weak_areas(student, 3)
        if weak:
            areas = ", ".join(w["area"].split(":", 1)[1] for w in weak)
            out.append(PlanItem("revision", "quiz", "Practise weak spots", areas))
        if not out:
            out.append(PlanItem("revision", "quiz", "Quick quiz",
                                "a short quiz on what was learned so far"))
        return out

    @staticmethod
    def describe(student: Student, plan: list[PlanItem], streak: int) -> str:
        lines = [f"Today's plan for {student.name}:"]
        labels = {"quran": "Quran", "dua": "Dua", "manners": "Manners", "revision": "Revision"}
        for item in plan:
            lines.append(f"* {labels[item.part]}: {item.title} - {item.detail}")
        if streak > 1:
            lines.append(f"{streak} days in a row - keep it up!")
        return "\n".join(lines)
