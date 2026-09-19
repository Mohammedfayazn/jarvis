"""Profiles must be findable by the name that was SPOKEN, not only by the
exact string they were stored under.

The real bug this pins down: "Zunaira Fatima" was in the database, Jarvis
looked up "zunaira" (it lowercases what it heard), the lookup was an exact
case-sensitive match, and Jarvis answered "no profile yet - add one first".
The parent then re-registered the child, over and over.

    python -m unittest discover tests
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from islamic.tracker import match_name  # noqa: E402
from speech_coach import SpeechCoach  # noqa: E402
from speech_coach.tracker import match_name as coach_match_name  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_islamic import make_tutor  # noqa: E402


class MatchNameTest(unittest.TestCase):
    def test_case_and_first_name(self):
        for match in (match_name, coach_match_name):
            self.assertTrue(match("zunaira", "Zunaira Fatima"), match)
            self.assertTrue(match("Zunaira Fatima", "zunaira fatima"), match)
            self.assertTrue(match("  fayaz ", "Fayaz"), match)
            self.assertTrue(match("fatima", "Zunaira Fatima"), match)
            self.assertFalse(match("ali", "Zunaira Fatima"), match)
            self.assertFalse(match("", "Zunaira"), match)
            self.assertFalse(match("zunaira", ""), match)

    def test_a_longer_spoken_name_does_not_match_a_shorter_profile(self):
        self.assertFalse(match_name("Zunaira Fatima", "Zunaira Khan"))


class CoachProfileTest(unittest.TestCase):
    def setUp(self):
        self.coach = SpeechCoach(":memory:")
        self.coach.add_child("Zunaira Fatima", 4, ["nl", "en", "hi"], "en")

    def test_first_name_finds_the_profile(self):
        for spoken in ("Zunaira", "zunaira", "ZUNAIRA", "zunaira fatima", "Zunaira  Fatima"):
            child, error = self.coach._child(spoken)
            self.assertIsNone(error, spoken)
            self.assertEqual(child.name, "Zunaira Fatima", spoken)

    def test_registering_again_updates_instead_of_duplicating(self):
        result = self.coach.add_child("Zunaira", 4, ["nl", "en", "hi"], "en")
        self.assertTrue(result["ok"])
        self.assertFalse(result["created"])                    # found the old one
        self.assertEqual(len(self.coach.tracker.children()), 1)

    def test_unknown_name_still_asks_for_a_profile(self):
        child, error = self.coach._child("Imran")
        self.assertIsNone(child)
        self.assertTrue(error["needs_profile"])

    def test_two_children_sharing_a_first_name_ask_which(self):
        self.coach.add_child("Zunaira Khan", 6, ["en"], "en")
        child, error = self.coach._child("Zunaira")
        self.assertIsNone(child)
        self.assertTrue(error["needs_choice"])
        self.assertIn("Zunaira Fatima", error["message"])
        # the full name still resolves
        self.assertEqual(self.coach._child("Zunaira Khan")[0].name, "Zunaira Khan")


class TutorProfileTest(unittest.TestCase):
    def setUp(self):
        self.tutor = make_tutor()[0]
        self.tutor.add_learner("Fayaz", role="parent")
        self.tutor.add_learner("Zunaira Fatima", role="child", age=4)

    def test_spoken_names_find_the_learner(self):
        for spoken, expected in (("fayaz", "Fayaz"), ("Fayaz", "Fayaz"),
                                 ("zunaira", "Zunaira Fatima"),
                                 ("Zunaira Fatima", "Zunaira Fatima")):
            student, error = self.tutor._student(spoken)
            self.assertIsNone(error, spoken)
            self.assertEqual(student.name, expected, spoken)

    def test_adding_a_known_learner_again_keeps_one_profile(self):
        before = len(self.tutor.tracker.students())
        self.tutor.add_learner("zunaira", role="child", age=4)
        self.assertEqual(len(self.tutor.tracker.students()), before)


if __name__ == "__main__":
    unittest.main()
