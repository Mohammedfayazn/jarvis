"""Islamic tutor tests.

Offline tests run on a fixture captured from the verified sources through
islamic/sources.py (tests/fixtures/islamic_sources.json) - no Arabic in
this file is typed by hand except deliberately *wrong* recitations.
Online tests (skipped without internet) re-check the live sources: that
every curriculum hadith reference still says what the lesson claims, and
that the basmala handling still matches the API.
"""
import json
import random
import sqlite3
import tempfile
import unittest
import urllib.request
from pathlib import Path

from islamic import IslamicTutor, db
from islamic.child import ChildLearningMode
from islamic.curriculum import ALPHABET, DUAS, MANNERS, MEMORIZATION_ORDER, TAJWEED
from islamic.recitation import analyze, feedback, normalize, skeleton
from islamic.sources import HadithSource, QuranSource, clean_arabic
from islamic.tracker import LearningTracker

FIXTURE = Path(__file__).with_name("fixtures") / "islamic_sources.json"


def seeded_conn() -> sqlite3.Connection:
    conn = db.connect(":memory:")
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    for table in ("quran_surahs", "quran_ayahs", "hadith_cache"):
        rows = data[table]
        cols = list(rows[0])
        conn.executemany(
            f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
            [tuple(r[c] for c in cols) for r in rows])
    conn.commit()
    return conn


def online() -> bool:
    try:
        urllib.request.urlopen("https://api.alquran.cloud/v1/meta", timeout=5)
        return True
    except Exception:
        return False


ONLINE = online()


class FakeTranscriber:
    def __init__(self, text=""):
        self.text = text
        self.error = None

    def available(self):
        return True

    def load(self):
        return True

    def transcribe(self, pcm, sample_rate=16000):
        return self.text


class Hooks:
    def __init__(self):
        self.shown, self.played, self.notes, self.recording = [], [], [], None

    def display(self, payload):
        self.shown.append(payload)

    def play(self, files, repeat):
        self.played.append((list(files), repeat))
        return True

    def record(self, on_done, child, expected_seconds):
        self.recording = (on_done, child, expected_seconds)
        return True

    def notify(self, text):
        self.notes.append(text)


def make_tutor(hooks=None, transcriber=None):
    hooks = hooks or Hooks()
    tutor = IslamicTutor(":memory:", display=hooks.display, play=hooks.play,
                         record=hooks.record, notify=hooks.notify,
                         transcriber=transcriber or FakeTranscriber(),
                         audio_dir=Path(tempfile.mkdtemp()))
    # swap in the seeded cache
    tutor._conn.close()
    conn = seeded_conn()
    tutor._conn = conn
    for part in (tutor.quran, tutor.hadith, tutor.tracker):
        part._conn = conn
    # no network for audio in tests
    tutor.quran.audio_file = lambda ayah: Path(f"{getattr(ayah, 'global_number', ayah)}.mp3")
    return tutor, hooks


class SourcesTest(unittest.TestCase):
    def setUp(self):
        self.conn = seeded_conn()
        self.quran = QuranSource(self.conn)

    def test_basmala_not_part_of_ayah_one(self):
        for surah in (112, 113, 114, 108):
            first = normalize(self.quran.ayah(surah, 1).arabic).split()[0]
            self.assertNotEqual(first, "بسم", f"surah {surah}")
        self.assertEqual(normalize(self.quran.ayah(1, 1).arabic).split()[0], "بسم")

    def test_clean_arabic_strips_bom_and_basmala(self):
        basmala = self.quran.ayah(1, 1).arabic
        ikhlas = self.quran.ayah(112, 1).arabic
        self.assertEqual(clean_arabic("﻿" + basmala + " " + ikhlas, 112, 1, basmala), ikhlas)
        self.assertEqual(clean_arabic(basmala, 1, 1, None), basmala)
        # ayah 2 is never touched
        self.assertEqual(clean_arabic(basmala + " x", 112, 2, basmala), basmala + " x")

    def test_resolve_surah_names(self):
        for name, number in [("Al-Ikhlas", 112), ("ikhlaas", 112), ("surah falaq", 113),
                             ("An-Nas", 114), ("112", 112), ("Al Kawthar", 108), ("fatiha", 1),
                             ("Al-Asr", 103), ("An-Nasr", 110)]:
            self.assertEqual(self.quran.surah(name).number, number, name)
        with self.assertRaises(ValueError):
            self.quran.surah("banana")

    def test_hadith_grading_labels(self):
        hadith = HadithSource(self.conn)
        self.assertIn("Sahih", hadith.get("bukhari", 5376).grading)
        self.assertIn("Sahih", hadith.get("tirmidhi", 1956).grading)
        self.assertEqual(hadith.get("Sahih al-Bukhari", 5376).number, 5376)
        with self.assertRaises(ValueError):
            hadith.get("madeupcollection", 1)


class CurriculumTest(unittest.TestCase):
    def test_alphabet_is_complete_and_unique(self):
        self.assertEqual(len(ALPHABET), 28)
        self.assertEqual(len({l.char for l in ALPHABET}), 28)

    def test_every_reference_resolves_and_matches_its_topic(self):
        conn = seeded_conn()
        quran, hadith = QuranSource(conn), HadithSource(conn)
        for m in MANNERS:
            text = (hadith.get(*m.source_hadith).text_en if m.source_hadith
                    else quran.ayah(*m.source_quran).translation)
            self.assertIn(m.must_contain.lower(), text.lower(), m.key)
        for d in DUAS:
            if d.quran:
                quran.ayah(*d.quran)
            if d.hadith:
                hadith.get(*d.hadith)
        for lesson in TAJWEED:
            for surah, ayah, _ in lesson.examples:
                quran.ayah(surah, ayah)

    def test_memorization_order_is_short_surahs(self):
        self.assertEqual(MEMORIZATION_ORDER[:2], (1, 112))
        self.assertEqual(len(set(MEMORIZATION_ORDER)), len(MEMORIZATION_ORDER))


class RecitationTest(unittest.TestCase):
    def setUp(self):
        self.quran = QuranSource(seeded_conn())
        self.ikhlas = self.quran.ayahs(112)

    def ikhlas_plain(self):
        """The recogniser writes plain script; derive it from the verified text."""
        return " ".join(normalize(a.arabic) for a in self.ikhlas)

    def test_perfect_recitation(self):
        result = analyze(self.ikhlas_plain(), self.ikhlas, 12)
        self.assertEqual(result.accuracy, 1.0)
        self.assertTrue(result.clear)
        self.assertEqual(result.missing, [])

    def test_uthmani_and_plain_spelling_count_as_the_same(self):
        # "لم يكن له" vs Uthmani "لَّهُۥ" / "يَكُن" - spelling, not a mistake
        for a in self.ikhlas:
            for word in a.arabic.split():
                self.assertEqual(skeleton(word), skeleton(normalize(word)))

    def test_missing_words_and_swapped_letter(self):
        words = self.ikhlas_plain().split()
        words = [w for w in words if w not in ("لم", "يلد")]      # skip "lam yalid"
        words[3] = words[3].replace("ح", "ه")                      # ahad -> ahad with ه
        result = analyze(" ".join(words), self.ikhlas, 10)
        missing = [normalize(w.expected.text) for w in result.missing]
        self.assertEqual(missing, ["لم", "يلد"])
        self.assertIn("ح>ه", result.letters_confused)
        text = feedback(result, child=False)
        self.assertIn("said ه instead of ح", text)
        self.assertIn("tajweed", text)

    def test_unclear_audio_is_never_the_childs_fault(self):
        for transcript, seconds in [("", 5), ("كلام آخر تماما", 4), (self.ikhlas_plain(), 0.3)]:
            result = analyze(transcript, self.ikhlas, seconds)
            self.assertFalse(result.clear)
            self.assertIn("couldn't hear", feedback(result, child=True, name="Zunaira"))

    def test_child_feedback_is_gentle_and_short(self):
        words = self.ikhlas_plain().split()
        result = analyze(" ".join(words[:5]), self.ikhlas, 5)
        text = feedback(result, child=True, name="Zunaira")
        self.assertNotIn("wrong", text.lower())
        self.assertLessEqual(text.count("Let's practise") + text.count("Don't forget"), 2)

    def test_word_boundaries_differ_between_scripts(self):
        """Uthmani writes "yaa ayyuha" as one word; the recogniser as two."""
        kafirun = self.quran.ayahs(109, 1, 2)
        plain = [normalize(w) for a in kafirun for w in a.arabic.split()]
        yaa = plain[1]
        self.assertEqual(len(plain), 7)
        heard = [plain[0], yaa[:2], "ا" + yaa[2:]] + plain[2:]      # split in two
        result = analyze(" ".join(heard), kafirun, 6)
        self.assertEqual(result.accuracy, 1.0)
        self.assertEqual(result.letters_missed, {})
        joined = plain[:5] + [plain[5] + plain[6]]                   # two joined into one
        self.assertEqual(analyze(" ".join(joined), kafirun, 6).accuracy, 1.0)

    def test_skipped_short_word_is_not_glued_to_its_neighbour(self):
        kafirun = self.quran.ayahs(109, 1, 2)
        plain = [normalize(w) for a in kafirun for w in a.arabic.split()]
        heard = plain[:5] + plain[6:]                                # skip "ma"
        result = analyze(" ".join(heard), kafirun, 6)
        self.assertEqual([normalize(w.expected.text) for w in result.missing], [plain[5]])

    def test_extra_word_does_not_shift_the_rest(self):
        words = self.ikhlas_plain().split()
        words.insert(2, "آمين")
        result = analyze(" ".join(words), self.ikhlas, 12)
        statuses = [w.status for w in result.words]
        self.assertEqual(statuses.count("extra"), 1)
        self.assertEqual(statuses.count("correct"), len(result.words) - 1)


class TrackerTest(unittest.TestCase):
    def setUp(self):
        self.tracker = LearningTracker(seeded_conn())
        self.child, _ = self.tracker.add_student("Zunaira", "child", 4)
        self.parent, _ = self.tracker.add_student("Fayaz", "parent")

    def test_profiles_are_separate(self):
        self.tracker.set_cursor(self.child, "alphabet", index=3)
        self.assertEqual(self.tracker.cursor(self.parent, "alphabet"), {})
        self.assertTrue(self.child.is_child)
        self.assertFalse(self.parent.is_child)

    def test_role_from_age(self):
        kid, _ = self.tracker.add_student("Ayaan", "", 6)
        self.assertEqual(kid.role, "child")

    def test_spaced_repetition(self):
        self.tracker.record_recitation(self.child, 112, {1: 1.0, 2: 0.4})
        rows = {r["ayah"]: r for r in self.tracker.memorization_rows(self.child, 112)}
        self.assertEqual(rows[1]["strength"], 1)
        self.assertEqual(rows[2]["strength"], 0)
        for _ in range(3):
            self.tracker.record_recitation(self.child, 112, {a: 1.0 for a in (1, 2, 3, 4)})
        self.assertEqual(self.tracker.memorized_surahs(self.child, {112: 4}), [112])

    def test_weak_areas_recover(self):
        self.tracker.note_area(self.child, "letter:ح", False)
        self.assertEqual(self.tracker.weak_areas(self.child)[0]["area"], "letter:ح")
        self.tracker.note_area(self.child, "letter:ح", True)
        self.assertEqual(self.tracker.weak_areas(self.child), [])


class ChildModeTest(unittest.TestCase):
    def setUp(self):
        self.tracker = LearningTracker(seeded_conn())
        self.child, _ = self.tracker.add_student("Zunaira", "child", 4)
        self.mode = ChildLearningMode(self.tracker, rng=random.Random(7))

    def test_answer_by_number_or_name(self):
        quiz = self.mode.new_quiz(self.child, "letter_name")
        right = quiz.options[quiz.answer]
        self.assertEqual(self.mode.match_answer(quiz, right.lower()), quiz.answer)
        self.assertEqual(self.mode.match_answer(quiz, f"number {quiz.answer + 1}"), quiz.answer)
        self.assertEqual(self.mode.match_answer(quiz, "the second one"), 1)
        self.assertIsNone(self.mode.match_answer(quiz, "banana smoothie"))

    def test_wrong_answer_is_encouraged_and_tracked(self):
        quiz = self.mode.new_quiz(self.child, "letter_name")
        wrong = next(i for i in range(3) if i != quiz.answer)
        result = self.mode.answer(self.child, f"number {wrong + 1}")
        self.assertFalse(result["correct"])
        self.assertNotIn("wrong", result["message"].lower())
        self.assertTrue(self.tracker.weak_areas(self.child))

    def test_unclear_answer_repeats_question(self):
        self.mode.new_quiz(self.child, "dua")
        result = self.mode.answer(self.child, "hmm")
        self.assertFalse(result["understood"])
        self.assertIsNotNone(self.mode.active_quiz(self.child))


class TutorFlowTest(unittest.TestCase):
    def setUp(self):
        self.tutor, self.hooks = make_tutor()

    def test_needs_a_profile_first(self):
        self.assertTrue(self.tutor.lesson()["needs_profile"])

    def test_role_defaults_from_age_not_to_child(self):
        self.assertEqual(self.tutor.add_learner("Fayaz")["student"]["role"], "parent")
        self.assertEqual(self.tutor.add_learner("Zunaira", age=4)["student"]["role"], "child")

    def test_placeholder_names_are_refused(self):
        for name in ("User", "me", ""):
            self.assertFalse(self.tutor.add_learner(name)["ok"], name)
        self.assertEqual(self.tutor.learners()["students"], [])

    def test_child_lesson_flow(self):
        self.tutor.add_learner("Zunaira", "child", 4)
        self.tutor.add_learner("Fayaz", "parent")
        first = self.tutor.lesson("child")
        self.assertIn("Letters lesson 1", first["message"])
        self.assertEqual(self.hooks.shown[-1]["mode"], "letters")
        surah = self.tutor.lesson("Zunaira", surah="Al-Ikhlas")
        self.assertTrue(surah["ok"], surah["message"])
        self.assertEqual(self.hooks.played[-1][1], 3)          # children hear it 3x
        self.assertTrue(surah["lesson"]["ayahs"])
        # "continue yesterday's lesson" picks the surah back up
        again = self.tutor.lesson("Zunaira", mode="continue")
        self.assertEqual(again["lesson"]["key"], surah["lesson"]["key"])
        nxt = self.tutor.complete_lesson("Zunaira")
        self.assertIn("complete", nxt["message"])

    def test_parent_and_child_plans_differ(self):
        self.tutor.add_learner("Zunaira", "child", 4)
        self.tutor.add_learner("Fayaz", "parent")
        child_plan = self.tutor.daily_plan("Zunaira")["plan"]
        parent_plan = self.tutor.daily_plan("me")["plan"]
        self.assertEqual(child_plan[0]["track"], "alphabet")
        self.assertEqual(parent_plan[0]["track"], "tajweed")
        self.assertEqual({p["part"] for p in child_plan}, {"quran", "dua", "manners", "revision"})

    def test_recitation_test_end_to_end(self):
        ikhlas = self.tutor.quran.ayahs(112)
        plain = " ".join(normalize(a.arabic) for a in ikhlas)
        self.tutor.transcriber = FakeTranscriber(plain)
        self.tutor.add_learner("Zunaira", "child", 4)
        started = self.tutor.test_recitation("Zunaira", "Al-Ikhlas")
        self.assertTrue(started["listening"], started["message"])
        on_done, child, _seconds = self.hooks.recording
        self.assertTrue(child)
        self.assertEqual(self.hooks.shown[-1]["items"], [])     # hifz test hides the text
        self.tutor._finish_recitation(self.tutor.tracker.get_student("Zunaira"), ikhlas,
                                      "ok", b"\x00\x00" * 16000 * 8)
        self.assertIn("every word correctly", self.hooks.notes[-1])
        self.assertEqual(self.hooks.shown[-1]["mode"], "result")
        progress = self.tutor.progress("Zunaira")["message"]
        self.assertIn("Streak: 1", progress)

    def test_nobody_recited(self):
        self.tutor.add_learner("Zunaira", "child", 4)
        self.tutor._finish_recitation(self.tutor.tracker.get_student("Zunaira"),
                                      self.tutor.quran.ayahs(112), "no_speech", b"")
        self.assertIn("didn't start reciting", self.hooks.notes[-1])

    def test_sources_are_labelled(self):
        verse = self.tutor.sources("verse", surah="Al-Ikhlas", ayah=1, to_ayah=4)
        self.assertIn("QURAN 112:1", verse["message"])
        self.assertIn("Sahih International", verse["message"])
        hadith = self.tutor.sources("hadith", collection="bukhari", number=5376)
        self.assertIn("HADITH - Sahih al-Bukhari 5376", hadith["message"])
        topic = self.tutor.sources("topic", "eating")
        self.assertIn("LESSON - Eating nicely", topic["message"])

    def test_unreachable_source_is_admitted_not_improvised(self):
        self.tutor.add_learner("Fayaz", "parent")
        # Surah 67 is not in the offline fixture; with the network blocked the
        # tutor must refuse rather than teach from memory.
        import islamic.sources as sources
        real = sources._get_json

        def blocked(url):
            raise sources.SourceUnavailable("offline")
        sources._get_json = blocked
        try:
            result = self.tutor.lesson("Fayaz", surah=67)
        finally:
            sources._get_json = real
        self.assertFalse(result["ok"])
        self.assertIn("won't teach it from memory", result["message"])


@unittest.skipUnless(ONLINE, "needs internet")
class LiveSourcesTest(unittest.TestCase):
    """The fixture was captured from these sources; make sure they still agree."""

    def setUp(self):
        self.conn = db.connect(":memory:")
        self.quran, self.hadith = QuranSource(self.conn), HadithSource(self.conn)

    def test_live_basmala_handling(self):
        self.assertEqual(normalize(self.quran.ayah(112, 1).arabic).split()[0], "قل")

    def test_live_manners_references(self):
        for m in MANNERS:
            if m.source_hadith:
                self.assertIn(m.must_contain.lower(), self.hadith.get(*m.source_hadith).text_en.lower(), m.key)

    def test_fixture_matches_live_text(self):
        data = json.loads(FIXTURE.read_text(encoding="utf-8"))
        cached = {(r["surah"], r["ayah"]): r["arabic"] for r in data["quran_ayahs"] if r["surah"] == 112}
        for ayah in self.quran.ayahs(112):
            self.assertEqual(cached[(112, ayah.ayah)], ayah.arabic)


if __name__ == "__main__":
    unittest.main()
