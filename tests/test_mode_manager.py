"""mode_manager ki phrase detection, switching aur session config - bina
Gemini ke. Chalao:  python -m unittest discover tests
"""
import sys
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from google.genai import types  # noqa: E402

import mode_manager as mm  # noqa: E402


def declarations(names):
    return [types.Tool(function_declarations=[
        types.FunctionDeclaration(name=n, description=n) for n in names])]


class DetectTest(unittest.TestCase):
    def test_to_child(self):
        for said in ("Jarvis, speak to my daughter",
                     "Jarvis talk to my daughter please",
                     "Activate Kids Mode",
                     "kids mode",
                     "child mode chalu karo",
                     "Zunaira se baat karo",
                     "beti se baat karo",
                     "बेटी से बात करो"):
            self.assertEqual(mm.detect(said), mm.CHILD, said)

    def test_to_adult(self):
        for said in ("Jarvis, back to me",
                     "adult mode",
                     "switch to normal mode",
                     "mujhse baat karo",
                     "stop kids mode",
                     "मुझसे बात करो"):
            self.assertEqual(mm.detect(said), mm.ADULT, said)

    def test_ordinary_talk_never_switches(self):
        for said in ("my daughter is sleeping",
                     "mode of transport",
                     "Zunaira ko khana do",
                     "child speech coach ka dashboard dikhao",
                     "send a WhatsApp to my daughter's school",
                     "", "   "):
            self.assertIsNone(mm.detect(said), said)

    def test_adult_wins_when_both_match(self):
        self.assertEqual(mm.detect("back to me, stop talking to my daughter"), mm.ADULT)


class SwitchTest(unittest.TestCase):
    def setUp(self):
        self.switched = []
        self.mode = mm.ModeManager(on_switch=self.switched.append)

    def test_switch_and_back(self):
        self.assertEqual(self.mode.key, mm.ADULT)
        result = self.mode.switch(mm.CHILD)
        self.assertTrue(result["changed"])
        self.assertEqual(self.mode.key, mm.CHILD)
        self.assertEqual([p.key for p in self.switched], [mm.CHILD])
        self.assertTrue(self.mode.switch("adult")["changed"])
        self.assertEqual(self.mode.key, mm.ADULT)

    def test_same_mode_does_not_reconnect(self):
        result = self.mode.switch("adult")
        self.assertTrue(result["ok"])
        self.assertFalse(result["changed"])
        self.assertEqual(self.switched, [])

    def test_synonyms_and_nonsense(self):
        self.assertTrue(self.mode.switch("kids")["changed"])
        self.assertEqual(self.mode.key, mm.CHILD)
        bad = self.mode.switch("teenager")
        self.assertFalse(bad["ok"])
        self.assertEqual(self.mode.key, mm.CHILD)      # unchanged

    def test_greeting_is_taken_once(self):
        self.assertIsNone(self.mode.take_greeting())
        self.mode.switch(mm.CHILD)
        self.assertIn("Zunaira", self.mode.take_greeting())
        self.assertIsNone(self.mode.take_greeting())

    def test_switch_is_thread_safe(self):
        # The set_mode tool runs on a worker thread while the loop reads
        done = threading.Barrier(5)

        def flip(target):
            self.mode.switch(target)
            done.wait(timeout=5)

        for i in range(4):
            threading.Thread(target=flip, args=([mm.ADULT, mm.CHILD][i % 2],)).start()
        done.wait(timeout=5)
        self.assertIn(self.mode.key, (mm.ADULT, mm.CHILD))


class ProfileConfigTest(unittest.TestCase):
    def setUp(self):
        self.mode = mm.ModeManager()
        self.base = types.LiveConnectConfig(
            response_modalities=[types.Modality.AUDIO],
            tools=declarations(["speech_coach", "practice_word", "send_whatsapp",
                                "close_window", "set_mode", "go_to_sleep"]),
        )

    def config(self, key):
        self.mode.switch(key)
        return self.mode.live_config(self.base, "ANSWER POLICY")

    def names(self, config):
        return [d.name for t in config.tools for d in (t.function_declarations or [])]

    def test_adult_pause_and_prompt(self):
        cfg = self.config(mm.ADULT)
        vad = cfg.realtime_input_config.automatic_activity_detection
        self.assertEqual(vad.silence_duration_ms, 1200)
        text = cfg.system_instruction.parts[0].text
        self.assertIn("180 words per minute", text)
        self.assertIn("ANSWER POLICY", text)

    def test_child_pause_and_prompt(self):
        cfg = self.config(mm.CHILD)
        vad = cfg.realtime_input_config.automatic_activity_detection
        self.assertEqual(vad.silence_duration_ms, 4000)
        self.assertEqual(vad.prefix_padding_ms, 600)
        self.assertEqual(vad.end_of_speech_sensitivity,
                         types.EndSensitivity.END_SENSITIVITY_LOW)
        text = cfg.system_instruction.parts[0].text
        self.assertIn("120 words per minute", text)
        self.assertIn("ONE or TWO short sentences", text)
        self.assertIn("ANSWER POLICY", text)

    def test_child_session_has_no_adult_tools(self):
        names = self.names(self.config(mm.CHILD))
        self.assertIn("practice_word", names)
        self.assertIn("set_mode", names)          # the way back
        self.assertIn("go_to_sleep", names)
        self.assertNotIn("send_whatsapp", names)
        self.assertNotIn("close_window", names)

    def test_adult_session_keeps_every_tool(self):
        self.assertEqual(len(self.names(self.config(mm.ADULT))), 6)

    def test_base_config_is_not_mutated(self):
        self.config(mm.CHILD)
        self.assertIsNone(self.base.realtime_input_config)
        self.assertEqual(len(self.names(self.base)), 6)

    def test_record_pause_floor(self):
        self.mode.switch(mm.CHILD)
        self.assertEqual(self.mode.record_pause(1.5), 4.0)
        self.assertEqual(self.mode.record_pause(6.0), 6.0)   # recitation stays longer
        self.mode.switch(mm.ADULT)
        self.assertEqual(self.mode.record_pause(1.0), 1.5)


if __name__ == "__main__":
    unittest.main()
