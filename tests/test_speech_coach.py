"""Child speech coach tests - no microphone, no speech model (a fake
transcriber stands in; real-model behaviour was measured separately)."""
import random
import re
import unittest

from speech_coach import DISCLAIMER, SpeechCoach
from speech_coach.conversation import ConversationEngine, analyze_utterance, detect_languages, tokenize
from speech_coach.pronunciation import FEEDBACK, compare, feedback
from speech_coach.storytelling import SCENES
from speech_coach.vocabulary import (
    CATEGORIES, LANGUAGES, WORDS, WORDS_BY_KEY, find_word, normalize_language, sound_spans,
)


class FakeTranscriber:
    def __init__(self):
        self.next = ""
        self.error = None

    def available(self):
        return True

    def load(self):
        return True

    def transcribe(self, pcm, language):
        return self.next


class Hooks:
    def __init__(self):
        self.shown, self.notes, self.recording = [], [], None

    def display(self, payload):
        self.shown.append(payload)

    def record(self, on_done, pause, max_seconds, no_speech):
        self.recording = (on_done, pause, max_seconds, no_speech)
        return True

    def notify(self, text):
        self.notes.append(text)


def make_coach():
    hooks, asr = Hooks(), FakeTranscriber()
    coach = SpeechCoach(":memory:", display=hooks.display, record=hooks.record,
                        notify=hooks.notify, transcriber=asr)
    coach.conversation.rng = random.Random(3)
    return coach, hooks, asr


class VocabularyTest(unittest.TestCase):
    def test_every_category_has_eight_words_in_every_language(self):
        for cat in CATEGORIES:
            words = [w for w in WORDS if w.category == cat]
            self.assertEqual(len(words), 8, cat)
            for w in words:
                for lang in LANGUAGES:
                    self.assertTrue(w.text(lang), (w.key, lang))
                self.assertTrue(re.search(r"[ऀ-ॿ]", w.hi), w.key)
                if cat != "colors":
                    self.assertIn(w.nl_article, ("de", "het"), w.key)

    def test_find_word_in_any_language(self):
        for spoken in ("dog", "de hond", "Hond", "कुत्ता", "kutta"):
            self.assertEqual(find_word(spoken).key, "dog", spoken)
        self.assertIsNone(find_word("spaceship"))

    def test_sound_spans_prefer_longest(self):
        self.assertEqual([s for s, _, _ in sound_spans("fish", "en")], ["f", "sh"])
        self.assertEqual([s for s, _, _ in sound_spans("schoen", "nl")], ["sch", "oe"])

    def test_normalize_language(self):
        self.assertEqual(normalize_language("Dutch"), "nl")
        self.assertEqual(normalize_language("nederlands"), "nl")
        self.assertEqual(normalize_language("klingon", "en"), "en")


class PronunciationTest(unittest.TestCase):
    """Transcripts here are what openai/whisper-base actually returned for
    synthesised test words (see the module docstring)."""

    def test_clear_word_even_inside_a_short_answer(self):
        for heard in ("Rabbit", "rabbit.", "a rabbit!", "It's a rabbit"):
            self.assertEqual(compare(WORDS_BY_KEY["rabbit"], "en", heard).status, "clear", heard)

    def test_r_to_w_is_attributed_to_r(self):
        attempt = compare(WORDS_BY_KEY["rabbit"], "en", "Wobbit")
        self.assertEqual(attempt.status, "close")
        self.assertEqual(attempt.sounds_missed, ("r",))
        self.assertIn("'r' sound", feedback(attempt))

    def test_dropped_syllable(self):
        attempt = compare(WORDS_BY_KEY["banana"], "en", "Nana")
        self.assertEqual(attempt.status, "close")
        self.assertIn("b", attempt.sounds_missed)

    def test_garbled_is_not_blamed_on_a_sound(self):
        attempt = compare(WORDS_BY_KEY["elephant"], "en", "Fland.")
        self.assertEqual(attempt.status, "different")
        self.assertEqual(attempt.sounds_missed, ())

    def test_nothing_heard_is_unclear(self):
        self.assertEqual(compare(WORDS_BY_KEY["dog"], "en", "").status, "unclear")

    def test_dutch_article_optional_and_hindi_script(self):
        self.assertEqual(compare(WORDS_BY_KEY["dog"], "nl", "De hond.").status, "clear")
        self.assertEqual(compare(WORDS_BY_KEY["dog"], "hi", "कुत्ता").status, "clear")

    def test_feedback_never_says_wrong(self):
        for lang, lines in FEEDBACK.items():
            for text in lines.values():
                self.assertNotIn("wrong", text.lower(), lang)
                self.assertNotIn("fout", text.lower(), lang)


class ConversationTest(unittest.TestCase):
    def test_short_sentence_gets_expansion_hints(self):
        info = analyze_utterance("Dog running", "en")
        self.assertTrue(info.short)
        self.assertEqual(info.words, 2)
        self.assertTrue(any("'is'" in h for h in info.hints))
        self.assertTrue(any("'the'" in h for h in info.hints))

    def test_long_sentence_no_hints(self):
        info = analyze_utterance("The dog is running in the park with me", "en")
        self.assertFalse(info.short)
        self.assertEqual(info.hints, [])

    def test_language_mixing(self):
        self.assertEqual(detect_languages(tokenize("the hond is running")), ["en", "nl"])
        self.assertIn("hi", detect_languages(tokenize("mama मैं पार्क गई")))
        # words shared by English and Dutch don't make a sentence "mixed"
        self.assertFalse(analyze_utterance("mama is in water", "en").mixed)

    def test_starters_in_every_language(self):
        engine = ConversationEngine(random.Random(1))
        for lang in LANGUAGES:
            topic, question = engine.starter(lang)
            self.assertTrue(question.strip())
            self.assertTrue(engine.follow_up(lang))


class StoryTest(unittest.TestCase):
    def test_scenes_complete_in_all_languages(self):
        for scene in SCENES:
            for lang in LANGUAGES:
                self.assertTrue(scene.title[lang] and scene.model_sentence[lang], (scene.key, lang))
                self.assertEqual(len(scene.questions[lang]), 3, (scene.key, lang))
            for key in scene.words:
                self.assertIn(key, WORDS_BY_KEY, scene.key)


class CoachFlowTest(unittest.TestCase):
    def setUp(self):
        self.coach, self.hooks, self.asr = make_coach()

    def test_needs_profile_and_refuses_placeholder(self):
        self.assertTrue(self.coach.words()["needs_profile"])
        self.assertFalse(self.coach.add_child("child")["ok"])
        added = self.coach.add_child("Zunaira", 4, ["nl", "en", "hi"], "nl")
        self.assertEqual(added["child"]["focus_language"], "nl")

    def test_full_ten_minute_session(self):
        self.coach.add_child("Zunaira", 4, ["nl", "en", "hi"], "en")
        start = self.coach.session("start")
        self.assertEqual([p["part"] for p in start["session"]],
                         ["greeting", "vocabulary", "pronunciation", "conversation", "story", "reward"])
        self.assertEqual(sum(p["minutes"] for p in start["session"]), 10)
        modes = []
        for _ in range(5):
            result = self.coach.session("next")
            self.assertTrue(result["ok"], result["message"])
            modes.append(self.hooks.shown[-1]["mode"])
        self.assertEqual(modes[0], "words")
        self.assertIn("scene", modes)
        self.assertEqual(modes[-1], "reward")
        self.assertEqual(self.coach.tracker.get_child("Zunaira").stars, 5)

    def test_word_practice_end_to_end(self):
        self.coach.add_child("Zunaira", 4, ["en", "nl"], "en")
        started = self.coach.practice_word(None, "rabbit")
        self.assertTrue(started["listening"])
        on_done, pause, _max, _silence = self.hooks.recording
        self.assertLess(pause, 2.5)                         # single word: short pause
        child = self.coach.tracker.get_child("Zunaira")
        self.asr.next = "Wobbit"
        self.coach._finish_word(child, WORDS_BY_KEY["rabbit"], "en", "ok", b"\x00\x00" * 8000)
        self.assertIn("Good try", self.hooks.notes[-1])
        self.assertEqual(self.coach.tracker.sounds(child)[0]["sound"] in ("r", "b"), True)
        self.coach._finish_word(child, WORDS_BY_KEY["rabbit"], "en", "no_speech", b"")
        self.assertIn("that's fine", self.hooks.notes[-1])

    def test_word_learned_after_two_clear_attempts(self):
        self.coach.add_child("Zunaira", 4, ["nl"], "nl")
        child = self.coach.tracker.get_child("Zunaira")
        dog = WORDS_BY_KEY["dog"]
        self.coach.check_word(child, dog, "nl", "hond")
        self.assertEqual(self.coach.tracker.words_learned_since(child, "2000-01-01"), [])
        self.coach.check_word(child, dog, "nl", "de hond")
        self.assertEqual(self.coach.tracker.words_learned_since(child, "2000-01-01"),
                         [{"word_key": "dog", "language": "nl"}])

    def test_unclear_audio_does_not_count_against_sounds(self):
        self.coach.add_child("Zunaira", 4, ["en"], "en")
        child = self.coach.tracker.get_child("Zunaira")
        for _ in range(5):
            self.coach.check_word(child, WORDS_BY_KEY["rabbit"], "en", "")
        self.assertEqual(self.coach.tracker.sounds(child), [])

    def test_sentences_observed_only_during_a_session(self):
        self.coach.add_child("Zunaira", 4, ["en", "nl"], "en")
        child = self.coach.tracker.get_child("Zunaira")
        self.coach.observe("dog running")
        self.assertEqual(self.coach.tracker.utterances(child, "2000-01-01"), [])
        self.coach.session("start")
        self.coach.observe("dog running")
        self.coach.sentence(None, "cat sleep", "The cat is sleeping on the bed.")
        rows = self.coach.tracker.utterances(child, "2000-01-01")
        self.assertEqual([r["words"] for r in rows], [2, 2])
        self.assertEqual(rows[1]["expanded"], "The cat is sleeping on the bed.")

    def test_parent_dashboard(self):
        self.coach.add_child("Zunaira", 4, ["nl", "en", "hi"], "nl")
        child = self.coach.tracker.get_child("Zunaira")
        self.coach.session("start")
        for text in ("dog running", "the hond is big", "mama I play with ball in the garden"):
            self.coach.observe(text)
        for _ in range(3):
            self.coach.check_word(child, WORDS_BY_KEY["rabbit"], "en", "Wobbit")
        self.coach.check_word(child, WORDS_BY_KEY["dog"], "nl", "hond")
        self.coach.check_word(child, WORDS_BY_KEY["dog"], "nl", "hond")
        report = self.coach.parent_dashboard()
        text = report["message"]
        self.assertIn("hond", text)
        self.assertIn("longest", text)
        self.assertIn("'r'", text)                            # r tried 3x, not yet heard
        self.assertTrue(text.strip().endswith(DISCLAIMER))
        for banned in ("delay", "disorder", "wrong", "behind"):
            self.assertNotIn(banned, text.lower())

    def test_translate(self):
        result = self.coach.translate("kutta")
        self.assertIn("hond", result["message"])
        self.assertIn("dog", result["message"])
        self.assertFalse(self.coach.translate("spaceship")["found"])


if __name__ == "__main__":
    unittest.main()
