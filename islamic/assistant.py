"""Application layer: the Islamic tutor Jarvis talks to.

Composes the knowledge base, teacher, child mode, recitation analyzer,
tracker and planner into use cases ("start Zunaira's lesson", "test Surah
Al-Ikhlas", "daily Islamic lesson"). Every public method returns a dict
with a "message" - what actually happened - like every other Jarvis tool.

It talks to the outside world only through hooks, so it runs the same in
the voice assistant, the CLI and the tests:
    display(payload)                 show something on the HUD
    play(files, repeat)              play recitation audio
    record(on_done, child, seconds)  capture a recitation from the mic
    notify(text)                     tell Jarvis something that happened later
"""
from __future__ import annotations

import logging
import threading
from pathlib import Path

from . import db
from .child import ChildLearningMode
from .knowledge import IslamicKnowledgeBase
from .planner import LessonPlanner
from .recitation import Transcriber, analyze, feedback
from .sources import HadithSource, QuranSource, SourceUnavailable
from .teacher import TRACKS, QuranTeacher, _verse_items
from .tracker import LearningTracker, Student

logger = logging.getLogger("jarvis.islamic")

_CHILD_WORDS = {"child", "my child", "kid", "the kid", "beti", "beta", "daughter", "son",
                "bachcha", "bachche", "bacha", "child's", "kids"}
_SELF_WORDS = {"me", "myself", "parent", "i", "abbu", "ammi", "papa", "mama", "mine"}


_PLACEHOLDER_NAMES = {"user", "me", "parent", "child", "kid", "learner", "student",
                      "unknown", "name", "myself", "someone", "the user", ""}


def _noop(*_args, **_kwargs):
    return False


class IslamicTutor:
    def __init__(self, db_path=None, *, display=None, play=None, record=None, notify=None,
                 transcriber: Transcriber | None = None, audio_dir: Path | None = None):
        path = Path(db_path) if db_path else db.DEFAULT_DB_PATH
        self._conn = db.connect(path)
        self.quran = QuranSource(self._conn, audio_dir=audio_dir)
        self.hadith = HadithSource(self._conn)
        self.kb = IslamicKnowledgeBase(self.quran, self.hadith)
        self.tracker = LearningTracker(self._conn)
        self.teacher = QuranTeacher(self.quran, self.hadith, self.tracker)
        self.child_mode = ChildLearningMode(self.tracker)
        self.planner = LessonPlanner(self.tracker, self.quran)
        self.transcriber = transcriber or Transcriber()
        self.display = display or _noop
        self.play_audio = play or _noop
        self.record = record or _noop
        self.notify = notify or _noop
        self._active: int | None = None
        self._lock = threading.Lock()

    def close(self) -> None:
        self._conn.close()

    # -- who is learning ---------------------------------------------------

    def _student(self, name: str | None):
        """-> (student, None) or (None, error_result)."""
        key = " ".join((name or "").lower().replace("'s", "").split())
        students = self.tracker.students()
        if not students:
            return None, {"ok": False, "needs_profile": True, "message":
                          "No learner profiles yet. Tell me the learner's name, whether they "
                          "are a parent or a child, and a child's age - then I'll set it up."}
        if key in _CHILD_WORDS:
            kids = [s for s in students if s.is_child]
            if len(kids) == 1:
                return self._use(kids[0]), None
            if not kids:
                return None, {"ok": False, "needs_profile": True,
                              "message": "There's no child profile yet - what is your child's name and age?"}
            return None, {"ok": False, "needs_choice": True,
                          "message": "Which child? " + ", ".join(k.name for k in kids)}
        if key in _SELF_WORDS:
            parents = [s for s in students if not s.is_child]
            if len(parents) == 1:
                return self._use(parents[0]), None
            if not parents:
                return None, {"ok": False, "needs_profile": True,
                              "message": "There's no parent profile yet - what's your name?"}
            return None, {"ok": False, "needs_choice": True,
                          "message": "Which of you? " + ", ".join(p.name for p in parents)}
        if key:
            # "fayaz" finds "Fayaz", "Zunaira" finds "Zunaira Fatima"
            found = self.tracker.find_students(key)
            if len(found) > 1:
                return None, {"ok": False, "needs_choice": True,
                              "message": "Which one? " + ", ".join(s.name for s in found)}
            if found:
                return self._use(found[0]), None
            return None, {"ok": False, "needs_profile": True, "message":
                          f"I don't have a profile for {name}. Should I add them? Tell me if "
                          "they're a child (and their age) or a parent."}
        if self._active:
            student = next((s for s in students if s.id == self._active), None)
            if student:
                return student, None
        if len(students) == 1:
            return self._use(students[0]), None
        return None, {"ok": False, "needs_choice": True,
                      "message": "Who is learning now? " + ", ".join(s.name for s in students)}

    def _use(self, student: Student) -> Student:
        self._active = student.id
        return student

    def _with(self, name, fn):
        student, error = self._student(name)
        if error:
            return error
        try:
            return fn(student)
        except SourceUnavailable as exc:
            return {"ok": False, "message":
                    f"I can't reach the verified Quran/hadith source right now ({exc}), and it "
                    "isn't saved offline yet - so I won't teach it from memory. Try again when "
                    "the internet is back."}
        except Exception as exc:
            logger.exception("islamic tutor failed")
            return {"ok": False, "message": f"Something went wrong: {exc}"}

    # -- profiles ------------------------------------------------------------

    def add_learner(self, name: str, role: str | None = None, age: int | None = None) -> dict:
        """Role defaults from age: under 13 is a child, no age means an adult."""
        if " ".join((name or "").lower().split()) in _PLACEHOLDER_NAMES:
            return {"ok": False, "message":
                    "I need the learner's real name - ask them what it is, then add them."}
        try:
            student, created = self.tracker.add_student(name, role, age)
        except ValueError as exc:
            return {"ok": False, "message": str(exc)}
        self._use(student)
        who = f"{student.name} ({student.role}" + (f", {student.age}" if student.age else "") + ")"
        return {"ok": True, "created": created, "student": student.as_dict(),
                "message": (f"Added {who}. Lessons will be sized for "
                            + ("a young child - short and playful." if student.is_child
                               else "an adult learner.")) if created
                else f"{who} is already set up."}

    def learners(self) -> dict:
        students = self.tracker.students()
        if not students:
            return {"ok": True, "students": [], "message": "No learners yet."}
        return {"ok": True, "students": [s.as_dict() for s in students],
                "message": "Learners: " + ", ".join(
                    f"{s.name} ({s.role}{', ' + str(s.age) if s.age else ''})" for s in students)}

    # -- lessons ---------------------------------------------------------

    def warm_up(self) -> None:
        """Load the speech model in the background (~15 s) so the first
        recitation test after a lesson doesn't keep the learner waiting."""
        if self.transcriber.available():
            threading.Thread(target=self.transcriber.load, name="asr-warmup", daemon=True).start()

    def lesson(self, student: str | None = None, track: str = "auto", mode: str = "continue",
               surah: int | None = None, ayah: int | None = None, play: bool = True) -> dict:
        self.warm_up()

        def run(s: Student) -> dict:
            chosen, how = (track or "auto").lower(), (mode or "continue").lower()
            if surah:
                chosen = "surah"
            elif chosen not in TRACKS:
                last = self.tracker.last_track(s) if how in ("continue", "yesterday") else None
                chosen = last[0] if last else self.planner.daily_plan(s)[0].track
            number = self.quran.surah(surah).number if surah else None
            lesson = self.teacher.lesson(s, chosen, "next" if how == "next" else "continue", number, ayah)
            played = False
            if play and lesson.audio:
                files = [self.quran.audio_file(n) for n in lesson.audio]
                played = bool(self.play_audio(files, lesson.repeat))
            self.display({"type": "lesson", **lesson.display, "student": s.name, "has_audio": played})
            self.tracker.log(s, "lesson", lesson.key)
            tail = ""
            if lesson.finished_track:
                tail = " (All the letters are done! Next we'll move on to reading and surahs.)"
            if played:
                tail += f" Playing the recitation {lesson.repeat}x."
            return {"ok": True, "student": s.name, "lesson": lesson.as_dict(),
                    "message": f"{s.name} - {lesson.speech}{tail}"}
        return self._with(student, run)

    def complete_lesson(self, student: str | None = None) -> dict:
        """The learner finished the current lesson: mark it and move on."""
        def run(s: Student) -> dict:
            last = self.tracker.last_track(s)
            if not last:
                return {"ok": False, "message": f"{s.name} hasn't started a lesson yet."}
            track, _ = last
            current = self.teacher.lesson(s, track, "continue")
            self.teacher.mark_complete(s, current.key)
            nxt = self.teacher.lesson(s, track, "next")
            self.display({"type": "lesson", **nxt.display, "student": s.name})
            return {"ok": True, "message": f"{self.child_mode.praise() if s.is_child else 'Done.'} "
                                           f"{current.title} complete. Next: {nxt.title}."}
        return self._with(student, run)

    def play(self, surah: int, from_ayah: int = 1, to_ayah: int | None = None,
             repeat: int = 1, basmala: bool = True) -> dict:
        try:
            info = self.quran.surah(surah)
            ayahs = self.quran.ayahs(info.number, int(from_ayah or 1), to_ayah or info.ayah_count)
            files = [self.quran.audio_file(a) for a in ayahs]
            if basmala and ayahs[0].ayah == 1 and info.number not in (1, 9):
                files.insert(0, self.quran.audio_file(1))
        except (ValueError, SourceUnavailable) as exc:
            return {"ok": False, "message": f"Couldn't load the recitation: {exc}"}
        span = f"{ayahs[0].ayah}-{ayahs[-1].ayah}" if len(ayahs) > 1 else str(ayahs[0].ayah)
        played = self.play_audio(files, repeat)
        self.display({"type": "lesson", "mode": "verses", "title": f"{info.name_en} · {info.name_ar}",
                      "subtitle": f"Ayah {span} · Mishary Alafasy", "items": _verse_items(ayahs),
                      "has_audio": bool(played)})
        return {"ok": True, "message": f"Surah {info.name_en}, ayah {span}"
                + (f", played {repeat}x." if played else " is on the screen (audio unavailable here).")}

    # -- quizzes -------------------------------------------------------------

    def quiz(self, student: str | None = None, answer: str | None = None,
             kind: str | None = None) -> dict:
        def run(s: Student) -> dict:
            if answer:
                result = self.child_mode.answer(s, answer)
                if result.get("display"):
                    self.display({"type": "lesson", **result.pop("display")})
                return result
            cursor = self.tracker.cursor(s, "surah")
            ayahs = []
            if cursor.get("surah"):
                ayahs = self.quran.ayahs(cursor["surah"], 1, cursor.get("ayah", 1) + cursor.get("span", 1) - 1)
            q = self.child_mode.new_quiz(s, kind, ayahs)
            self.display({"type": "lesson", **q.display()})
            return {"ok": True, "quiz": {"question": q.question, "options": q.options},
                    "message": f"Quiz for {s.name}: {q.spoken()}"}
        return self._with(student, run)

    # -- recitation ----------------------------------------------------------

    def test_recitation(self, student: str | None = None, surah: int | None = None,
                        from_ayah: int | None = None, to_ayah: int | None = None,
                        show_text: bool = False) -> dict:
        def run(s: Student) -> dict:
            if not self.transcriber.available():
                return {"ok": False, "message":
                        "Recitation checking needs the local speech model. Install it with "
                        f"'{self.transcriber.error}' in Jarvis's environment."}
            if surah:
                info = self.quran.surah(surah)
                start, end = int(from_ayah or 1), int(to_ayah or info.ayah_count)
            else:
                cur = self.tracker.cursor(s, "surah")
                if not cur.get("surah"):
                    return {"ok": False, "message": "Which surah should I test?"}
                info = self.quran.surah(cur["surah"])
                start, end = 1, cur.get("ayah", 1) + cur.get("span", 1) - 1
            ayahs = self.quran.ayahs(info.number, start, end)
            if not self.transcriber.load():
                return {"ok": False, "message": f"The speech model failed to load: {self.transcriber.error}"}
            words = sum(len(a.arabic.split()) for a in ayahs)
            span = f"{start}-{end}" if end > start else str(start)
            self.display({"type": "lesson", "mode": "recite", "student": s.name,
                          "title": f"{info.name_en} · {info.name_ar}",
                          "subtitle": f"{s.name}, recite ayah {span} now - I'm listening",
                          "items": _verse_items(ayahs) if show_text else []})

            def done(status: str, pcm: bytes) -> None:
                threading.Thread(target=self._finish_recitation, name="recitation-check",
                                 args=(s, ayahs, status, pcm), daemon=True).start()

            started = self.record(done, child=s.is_child, expected_seconds=words * 0.8)
            if not started:
                return {"ok": False, "message": "I can't record here - recitation tests need the live assistant."}
            return {"ok": True, "listening": True, "message":
                    f"Recording now. Tell {s.name} to recite Surah {info.name_en}, ayah {span}. "
                    "Stay completely silent while they recite - I'll send you the result "
                    "when they finish."}
        return self._with(student, run)

    def check_transcript(self, student: Student, ayahs, transcript: str,
                         audio_seconds: float | None = None) -> dict:
        """Analyse a transcript, update progress and weak areas, word feedback."""
        result = analyze(transcript, ayahs, audio_seconds)
        text = feedback(result, child=student.is_child, name=student.name)
        if result.clear:
            per_ayah: dict[int, list[float]] = {}
            credit = {"correct": 1.0, "close": 0.75, "wrong": 0.25, "missing": 0.0}
            for w in result.words:
                if w.expected:
                    per_ayah.setdefault(w.expected.ayah, []).append(credit[w.status])
            self.tracker.record_recitation(
                student, result.surah, {a: sum(v) / len(v) for a, v in per_ayah.items()})
            for pair, _count in result.letters_confused.items():
                self.tracker.note_area(student, f"letter:{pair.split('>')[0]}", False)
            for letter in result.letters_missed:
                self.tracker.note_area(student, f"letter:{letter}", False)
            for w in result.missing:
                self.tracker.note_area(student, f"word:{result.surah}:{w.expected.text}", False)
            # A weak letter only "recovers" in a recitation where it was not
            # also got wrong - mixed evidence in one go is not improvement.
            wrong_now = {p.split(">")[0] for p in result.letters_confused} | set(result.letters_missed)
            weak_letters = {a["area"].split(":", 1)[1] for a in self.tracker.weak_areas(student, 20)
                            if a["area"].startswith("letter:")} - wrong_now
            correct_text = " ".join(w.expected.text for w in result.words if w.status == "correct")
            for letter in weak_letters:
                if letter in correct_text:
                    self.tracker.note_area(student, f"letter:{letter}", True)
            self.tracker.log(student, "recitation", f"surah {result.surah} {result.ayahs}", result.accuracy)
        self.display({"type": "lesson", "mode": "result", "student": student.name,
                      "title": f"{student.name}'s recitation", "accuracy": round(result.accuracy * 100),
                      "clear": result.clear, "text": text,
                      "words": [{"text": w.expected.text if w.expected else w.heard, "status": w.status}
                                for w in result.words]})
        return {"ok": True, "result": result.as_dict(), "message": text}

    def _finish_recitation(self, student: Student, ayahs, status: str, pcm: bytes) -> None:
        if status != "ok":
            msg = (f"{student.name} didn't start reciting (I heard nothing for 15 seconds). "
                   "Ask if they're ready and offer to try again.")
            self.display({"type": "lesson", "mode": "feedback", "correct": False,
                          "title": "I didn't hear anything", "text": msg})
            self.notify(f"[Recitation check] {msg}")
            return
        seconds = len(pcm) / 32000
        self.display({"type": "lesson", "mode": "recite", "title": "Checking...",
                      "subtitle": "Listening back to the recitation", "items": []})
        try:
            transcript = self.transcriber.transcribe(pcm)
            # what the recogniser heard - kept in the log so a surprising
            # result can be checked, never spoken to the learner
            logger.info("recitation by %s, %.1fs, heard: %s", student.name, seconds, transcript)
            outcome = self.check_transcript(student, ayahs, transcript, seconds)
            self.notify("[Recitation result - tell it kindly in your own words; do not read "
                        f"any Arabic transcript aloud] {outcome['message']}")
        except Exception as exc:
            logger.exception("recitation analysis failed")
            self.notify(f"[Recitation check failed: {exc}. Apologise and offer to try again.]")

    # -- plans and progress --------------------------------------------------

    def daily_plan(self, student: str | None = None) -> dict:
        def run(s: Student) -> dict:
            plan = self.planner.daily_plan(s)
            text = self.planner.describe(s, plan, self.tracker.streak(s))
            self.display({"type": "lesson", "mode": "plan", "title": f"Today · {s.name}",
                          "items": [i.as_dict() for i in plan]})
            self.tracker.log(s, "plan", "daily plan")
            return {"ok": True, "plan": [i.as_dict() for i in plan],
                    "message": text + "\nStart with the Quran part?"}
        return self._with(student, run)

    def progress(self, student: str | None = None) -> dict:
        def run(s: Student) -> dict:
            lengths = {x.number: x.ayah_count for x in self.quran.surahs()}
            names = {x.number: x.name_en for x in self.quran.surahs()}
            memorized = [names[n] for n in self.tracker.memorized_surahs(s, lengths)]
            completed = self.tracker.completed_lessons(s)
            last = self.tracker.last_track(s)
            current = "none yet"
            if last:
                track, pos = last
                current = (f"{names.get(pos.get('surah'), '?')} ayah {pos.get('ayah')}" if track == "surah"
                           else f"{track} lesson {pos.get('index', 0) + 1}")
            weak = [w["area"].split(":", 1)[1] if not w["area"].startswith("word:")
                    else w["area"].split(":", 2)[2] for w in self.tracker.weak_areas(s)]
            right, total = self.child_mode.today_score(s)
            lines = [f"{s.name} ({s.role}{', ' + str(s.age) if s.age else ''}):",
                     f"* Current lesson: {current}",
                     f"* Completed lessons: {len(completed)}",
                     f"* Memorised surahs: {', '.join(memorized) or 'none yet'}",
                     f"* Needs practice: {', '.join(weak) or 'nothing stands out'}",
                     f"* Streak: {self.tracker.streak(s)} day(s)"]
            if total:
                lines.append(f"* Today's quiz: {right} of {total} right")
            return {"ok": True, "student": s.as_dict(), "completed_lessons": completed,
                    "memorized_surahs": memorized, "weak_areas": weak, "message": "\n".join(lines)}
        return self._with(student, run)

    # -- knowledge -------------------------------------------------------------

    def sources(self, action: str, query: str | None = None, surah: int | None = None,
                ayah: int | None = None, to_ayah: int | None = None,
                collection: str | None = None, number: int | None = None) -> dict:
        action = (action or "").lower()
        if action in ("verse", "quran", "quran_verse", "ayah"):
            if not surah:
                return {"ok": False, "message": "Which surah and ayah?"}
            try:
                number = self.quran.surah(surah).number
            except (ValueError, SourceUnavailable) as exc:
                return {"ok": False, "message": str(exc)}
            return self.kb.verses(number, int(ayah or 1), int(to_ayah) if to_ayah else None)
        if action in ("search", "quran_search"):
            return self.kb.search_quran(query or "")
        if action in ("hadith",):
            if not (collection and number):
                return {"ok": False, "message": "Which collection and hadith number?"}
            return self.kb.hadith(collection, int(number))
        if action in ("topic", "manners", "dua"):
            return self.kb.topic(query or action)
        return {"ok": False, "message": "Use action verse, search, hadith or topic."}
