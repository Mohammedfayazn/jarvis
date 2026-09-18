import tempfile
import unittest
from pathlib import Path

from memory import assistant, intent
from memory.models import normalize_category
from memory.service import MemoryManager


class MemoryManagerTest(unittest.TestCase):
    def setUp(self):
        self._tmp = Path(tempfile.mktemp(suffix=".db"))
        self.manager = MemoryManager(self._tmp)

    def tearDown(self):
        self.manager.close()
        self._tmp.unlink(missing_ok=True)

    def test_add_and_get_exact_key(self):
        self.manager.add_memory("Family", "daughter's school", "8:30")
        memory = self.manager.get_memory("daughter's school")
        self.assertIsNotNone(memory)
        self.assertEqual(memory.value, "8:30")
        self.assertEqual(memory.category, "Family")

    def test_get_is_case_and_punctuation_insensitive(self):
        self.manager.add_memory("Personal", "Office WiFi Password", "XYZ")
        memory = self.manager.get_memory("office wifi password")
        self.assertIsNotNone(memory)
        self.assertEqual(memory.value, "XYZ")

    def test_get_fuzzy_typo(self):
        self.manager.add_memory("Preferences", "favorite editor", "VS Code")
        memory = self.manager.get_memory("favorit editor")
        self.assertIsNotNone(memory)
        self.assertEqual(memory.value, "VS Code")

    def test_similar_keys_are_not_confused(self):
        self.manager.add_memory("Family", "daughter name", "Zunaira")
        self.manager.add_memory("Family", "daughter age", "4 years")
        self.manager.add_memory("Personal", "user name", "Fayaz")
        self.assertIsNone(self.manager.get_memory("wife name"))
        self.assertTrue(self.manager.delete_memory("daughter age"))
        self.assertEqual(self.manager.get_memory("daughter name").value, "Zunaira")

    def test_add_memory_twice_updates_in_place(self):
        self.manager.add_memory("Personal", "wifi password", "old")
        self.manager.add_memory("Personal", "wifi password", "new")
        memories = self.manager.list_memories()
        self.assertEqual(len(memories), 1)
        self.assertEqual(memories[0].value, "new")

    def test_update_memory(self):
        self.manager.add_memory("Personal", "wifi password", "old")
        updated = self.manager.update_memory("wifi password", "new")
        self.assertIsNotNone(updated)
        self.assertEqual(updated.value, "new")

    def test_update_missing_key_returns_none(self):
        self.assertIsNone(self.manager.update_memory("nope", "value"))

    def test_delete_memory(self):
        self.manager.add_memory("Personal", "temp fact", "value")
        self.assertTrue(self.manager.delete_memory("temp fact"))
        self.assertIsNone(self.manager.get_memory("temp fact"))

    def test_delete_missing_key_returns_false(self):
        self.assertFalse(self.manager.delete_memory("nothing here"))

    def test_list_memories_by_category(self):
        self.manager.add_memory("Family", "daughter's school", "8:30")
        self.manager.add_memory("Work", "standup time", "9:00")
        family_only = self.manager.list_memories("Family")
        self.assertEqual(len(family_only), 1)
        self.assertEqual(family_only[0].key, "daughters_school")

    def test_search_memory_answers_question(self):
        self.manager.add_memory("Family", "daughter's school", "8:30")
        results = self.manager.search_memory(
            "What time does my daughter's school start?"
        )
        self.assertTrue(results)
        self.assertEqual(results[0][0].value, "8:30")

    def test_search_memory_no_match(self):
        self.manager.add_memory("Family", "daughter's school", "8:30")
        self.assertEqual(self.manager.search_memory("what is the weather"), [])


class NormalizeCategoryTest(unittest.TestCase):
    def test_known_category_case_insensitive(self):
        self.assertEqual(normalize_category("family"), "Family")

    def test_unknown_falls_back_to_personal(self):
        self.assertEqual(normalize_category("xyz nonsense"), "Personal")

    def test_close_typo_matches(self):
        self.assertEqual(normalize_category("Preferance"), "Preferences")

    def test_missing_defaults_to_personal(self):
        self.assertEqual(normalize_category(None), "Personal")


class ParseRememberCommandTest(unittest.TestCase):
    def test_daughters_school(self):
        result = intent.parse("Remember that my daughter's school starts at 8:30")
        self.assertEqual(result.action, "remember")
        self.assertEqual(result.key, "daughter's school")
        self.assertEqual(result.value, "8:30")
        self.assertEqual(result.category, "Family")

    def test_wifi_password(self):
        result = intent.parse("Remember my office WiFi password is XYZ")
        self.assertEqual(result.action, "remember")
        self.assertEqual(result.key, "office WiFi password")
        self.assertEqual(result.value, "XYZ")

    def test_favorite_editor(self):
        result = intent.parse("Remember my favorite editor is VS Code")
        self.assertEqual(result.action, "remember")
        self.assertEqual(result.key, "favorite editor")
        self.assertEqual(result.value, "VS Code")
        self.assertEqual(result.category, "Preferences")

    def test_live_in_amsterdam(self):
        result = intent.parse("Remember that I live in Amsterdam")
        self.assertEqual(result.action, "remember")
        self.assertEqual(result.key, "where I live")
        self.assertEqual(result.value, "Amsterdam")

    def test_question_detected(self):
        result = intent.parse("What time does my daughter's school start?")
        self.assertEqual(result.action, "query")

    def test_unrelated_sentence_is_none(self):
        result = intent.parse("Play some music please")
        self.assertEqual(result.action, "none")


class DetectStorableFactTest(unittest.TestCase):
    def test_detects_casual_fact(self):
        candidate = intent.detect_storable_fact(
            "My favorite programming language is Python."
        )
        self.assertIsNotNone(candidate)
        self.assertEqual(candidate.action, "extract_candidate")
        self.assertEqual(candidate.key, "favorite programming language")
        self.assertEqual(candidate.value, "Python")
        self.assertEqual(candidate.category, "Preferences")

    def test_explicit_remember_is_not_a_candidate(self):
        self.assertIsNone(
            intent.detect_storable_fact("Remember my favorite editor is VS Code")
        )

    def test_question_is_not_a_candidate(self):
        self.assertIsNone(intent.detect_storable_fact("What is my wifi password?"))

    def test_long_sentence_is_not_a_candidate(self):
        long_sentence = (
            "My plan for this weekend is to finally clean the garage, fix "
            "the fence, and maybe call my parents if there is time left."
        )
        self.assertIsNone(intent.detect_storable_fact(long_sentence))


class AssistantLayerTest(unittest.TestCase):
    def setUp(self):
        self._tmp = Path(tempfile.mktemp(suffix=".db"))
        assistant.configure(self._tmp)

    def tearDown(self):
        assistant._get_manager().close()
        self._tmp.unlink(missing_ok=True)

    def test_remember_then_recall(self):
        remembered = assistant.remember_this("Family", "daughter's school", "8:30")
        self.assertTrue(remembered["ok"])
        self.assertIn("8:30", remembered["message"])

        recalled = assistant.recall_memory("daughter's school start time")
        self.assertTrue(recalled["found"])
        self.assertIn("8:30", recalled["message"])

    def test_remember_twice_reports_update(self):
        assistant.remember_this("Personal", "wifi password", "old")
        second = assistant.remember_this("Personal", "wifi password", "new")
        self.assertIn("update", second["message"])

    def test_new_similar_key_reports_new_not_update(self):
        assistant.remember_this("Personal", "user name", "Fayaz")
        result = assistant.remember_this("Family", "wife name", "Firdos")
        self.assertIn("yaad rakh liya", result["message"])

    def test_recall_missing_reports_not_found(self):
        result = assistant.recall_memory("something never stored")
        self.assertFalse(result["found"])

    def test_forget_removes_memory(self):
        assistant.remember_this("Personal", "temp fact", "value")
        forgotten = assistant.forget_memory("temp fact")
        self.assertTrue(forgotten["ok"])
        self.assertFalse(assistant.recall_memory("temp fact")["found"])

    def test_forget_missing_reports_failure(self):
        result = assistant.forget_memory("never existed")
        self.assertFalse(result["ok"])

    def test_list_memories_empty(self):
        result = assistant.list_memories()
        self.assertEqual(result["count"], 0)

    def test_list_memories_after_adding(self):
        assistant.remember_this("Family", "daughter's school", "8:30")
        assistant.remember_this("Work", "standup time", "9:00")
        result = assistant.list_memories()
        self.assertEqual(result["count"], 2)

    def test_missing_key_or_value_reports_failure(self):
        result = assistant.remember_this("Personal", "", "value")
        self.assertFalse(result["ok"])


if __name__ == "__main__":
    unittest.main()
