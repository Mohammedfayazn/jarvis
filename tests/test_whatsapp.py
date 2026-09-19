"""whatsapp_handler ki parsing, filtering, summary aur send confirmation -
bina browser ke. Chalao:  python -m unittest discover tests
"""
import datetime as dt
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import whatsapp_handler as wa  # noqa: E402
from whatsapp_handler import ChatRow, WhatsAppError  # noqa: E402

NOW = dt.datetime(2026, 9, 19, 14, 0)


def raw(name, time, snippet, unread=None, icons=()):
    badges = [{"label": f"{unread} unread messages", "text": str(unread)}] if unread else []
    return {"titles": [name, snippet], "lines": [name, time, snippet] + ([str(unread)] if unread else []),
            "badges": badges, "icons": list(icons)}


class TextSafetyTest(unittest.TestCase):
    def test_devanagari_and_emoji_survive(self):
        for text in ("नमस्ते, आप कैसे हैं? 🙏", "Kal milte hain bhai 👍🏽", "👨‍👩‍👧 family"):
            self.assertEqual(wa.clean_text(text), text)

    def test_nfc_normalised(self):
        decomposed = "José"          # e + combining acute
        self.assertEqual(wa.clean_text(decomposed), "José")

    def test_control_chars_and_lone_surrogates(self):
        self.assertEqual(wa.clean_text("hi\x00 there\x07"), "hi there")
        self.assertEqual(wa.clean_text("a\ud800b"), "a?b")
        self.assertEqual(wa.clean_text("line one\nline two"), "line one\nline two")

    def test_direction_marks_removed(self):
        # WhatsApp's title attributes come wrapped in LRE ... PDF
        self.assertEqual(wa.clean_text("\u202aBaqi ghar pay sab theek hain?\u202c"),
                         "Baqi ghar pay sab theek hain?")
        self.assertEqual(wa.clean_text("a\u200db"), "a\u200db")   # ZWJ stays

    def test_letters_only_tolerates_emoticons_but_not_lost_matras(self):
        self.assertEqual(wa.letters_only("Theek hai :)"), wa.letters_only("Theek hai 🙂"))
        self.assertNotEqual(wa.letters_only("नमस्ते"), wa.letters_only("नमसत"))


class PhoneTest(unittest.TestCase):
    def test_international_numbers(self):
        self.assertEqual(wa.phone_digits("+91 98765 43210"), "919876543210")
        self.assertEqual(wa.phone_digits("0031 6 1234 5678"), "31612345678")
        self.assertEqual(wa.phone_digits("+31-6-1234-5678"), "31612345678")

    def test_names_are_not_numbers(self):
        self.assertIsNone(wa.phone_digits("Rahul"))
        self.assertIsNone(wa.phone_digits("Ammi 2"))

    def test_no_country_code_is_refused(self):
        with self.assertRaises(WhatsAppError) as ctx:
            wa.phone_digits("06 1234 5678")
        self.assertEqual(ctx.exception.status, "invalid_number")


class MatchContactTest(unittest.TestCase):
    RESULTS = ["Rahul Sharma", "Priya", "Rahul Office", "राहुल भाई", "Ammi"]

    def test_exact_beats_partial(self):
        self.assertEqual(wa.match_contact("priya", self.RESULTS), ("found", ["Priya"]))
        self.assertEqual(wa.match_contact("ammi", self.RESULTS + ["Ammi Jaan"]),
                         ("found", ["Ammi"]))

    def test_partial_single(self):
        self.assertEqual(wa.match_contact("rahul sharma", self.RESULTS),
                         ("found", ["Rahul Sharma"]))

    def test_ambiguous(self):
        status, names = wa.match_contact("rahul", self.RESULTS)
        self.assertEqual(status, "ambiguous")
        self.assertEqual(names, ["Rahul Sharma", "Rahul Office"])

    def test_devanagari_name(self):
        self.assertEqual(wa.match_contact("राहुल", self.RESULTS), ("found", ["राहुल भाई"]))

    def test_accents_ignored(self):
        self.assertEqual(wa.match_contact("jose", ["José Martín"]), ("found", ["José Martín"]))

    def test_not_found(self):
        self.assertEqual(wa.match_contact("Alex", self.RESULTS), ("not_found", []))


class TimeLabelTest(unittest.TestCase):
    def test_clock_formats(self):
        self.assertEqual(wa.classify_time_label("10:30", NOW)[1], NOW.replace(hour=10, minute=30))
        self.assertEqual(wa.classify_time_label("2:05 PM", NOW)[1], NOW.replace(hour=14, minute=5))
        self.assertEqual(wa.classify_time_label("12:10 am", NOW)[1], NOW.replace(hour=0, minute=10))
        self.assertEqual(wa.classify_time_label("09.15", NOW)[0], "today")

    def test_other_labels(self):
        self.assertEqual(wa.classify_time_label("Yesterday", NOW)[0], "yesterday")
        self.assertEqual(wa.classify_time_label("Gisteren", NOW)[0], "yesterday")
        self.assertEqual(wa.classify_time_label("Friday", NOW)[0], "older")
        self.assertEqual(wa.classify_time_label("12/09/2026", NOW)[0], "older")
        self.assertEqual(wa.classify_time_label("typing…", NOW)[0], "unknown")


class ParseRowTest(unittest.TestCase):
    def test_basic_row(self):
        row = wa.parse_row(raw("Rahul", "10:30", "Are we meeting today at 5 PM?", unread=2))
        self.assertEqual((row.name, row.time_label, row.snippet, row.unread),
                         ("Rahul", "10:30", "Are we meeting today at 5 PM?", 2))
        self.assertFalse(row.from_me)

    def test_outgoing_and_pinned(self):
        row = wa.parse_row(raw("Priya", "11:15", "ok", icons=["status-dblcheck", "pinned2"]))
        self.assertTrue(row.from_me)
        self.assertTrue(row.pinned)

    def test_muted_is_not_outgoing(self):
        self.assertFalse(wa.parse_row(raw("Group", "11:15", "hi", icons=["muted"])).from_me)

    def test_marked_unread_without_count(self):
        r = raw("Ammi", "Yesterday", "Call me")
        r["badges"] = [{"label": "Unread", "text": ""}]
        row = wa.parse_row(r)
        self.assertTrue(row.is_unread)
        self.assertEqual(wa.row_to_message(row)["unread"], 1)

    def test_row_without_title_is_skipped(self):
        self.assertIsNone(wa.parse_row({"titles": [], "lines": ["Chats"]}))


class SelectRowsTest(unittest.TestCase):
    ROWS = [
        ChatRow("Rahul", "Are we meeting?", "13:30", unread=1),
        ChatRow("Priya", "Check the draft", "10:00"),
        ChatRow("Me-sent", "done", "12:00", from_me=True),
        ChatRow("Ammi", "Khana kha liya?", "Yesterday", unread=3),
        ChatRow("Old", "hello", "Monday"),
    ]

    def names(self, mode, hours=3, now=NOW):
        return [r.name for r in wa.select_rows(self.ROWS, mode, now, hours)]

    def test_unread(self):
        self.assertEqual(self.names("unread"), ["Rahul", "Ammi"])

    def test_today_skips_own_messages(self):
        self.assertEqual(self.names("today"), ["Rahul", "Priya"])

    def test_recent_window(self):
        self.assertEqual(self.names("recent", hours=3), ["Rahul"])
        self.assertEqual(self.names("recent", hours=5), ["Rahul", "Priya"])

    def test_recent_reaching_past_midnight_includes_yesterday(self):
        early = NOW.replace(hour=1)
        rows = [ChatRow("Late", "gn", "Yesterday"), ChatRow("Now", "hi", "00:30")]
        self.assertEqual([r.name for r in wa.select_rows(rows, "recent", early, 3)], ["Late", "Now"])
        self.assertEqual([r.name for r in wa.select_rows(rows, "recent", early, 0.75)], ["Now"])


class SummaryTest(unittest.TestCase):
    MESSAGES = [
        {"sender": "Rahul", "text": "Are we meeting at 5 PM?", "time": "10:30", "unread": 1},
        {"sender": "Priya", "text": "Please check the draft email.", "time": "11:15", "unread": 1},
    ]

    def test_today(self):
        self.assertEqual(
            wa.build_summary(self.MESSAGES, "today"),
            "You have 2 new messages from today. Rahul says: 'Are we meeting at 5 PM?', "
            "and Priya says: 'Please check the draft email.'. Would you like me to send "
            "a reply to any of these contacts?",
        )

    def test_unread_counts(self):
        msgs = [dict(self.MESSAGES[0], unread=3), self.MESSAGES[1]]
        summary = wa.build_summary(msgs, "unread")
        self.assertTrue(summary.startswith("You have 4 unread messages in 2 chats."))
        self.assertIn("Rahul sent 3 messages, the latest: 'Are we meeting at 5 PM?'", summary)

    def test_empty(self):
        self.assertEqual(wa.build_summary([], "unread"), "You have no unread WhatsApp messages.")
        self.assertIn("last 2 hours", wa.build_summary([], "recent", 2))

    def test_long_list_and_long_text(self):
        msgs = [{"sender": f"P{i}", "text": "word " * 60, "unread": 1} for i in range(9)]
        summary = wa.build_summary(msgs, "today")
        self.assertIn("and 3 more chats.", summary)
        self.assertIn("...'", summary)

    def test_links_and_formatting_are_speakable(self):
        msgs = [{"sender": "Ammi", "unread": 0,
                 "text": "*Zaroor dekho* https://youtu.be/zeSkNF4Ngkk?si=abc _abhi_"}]
        self.assertIn("Ammi says: 'Zaroor dekho a link abhi'", wa.build_summary(msgs, "today"))
        msgs = [{"sender": "Raj", "unread": 0, "text": "file_name_v2 and 2*3"}]
        self.assertIn("'file_name_v2 and 2*3'", wa.build_summary(msgs, "today"))

    def test_hindi_text_kept(self):
        msgs = [{"sender": "राहुल", "text": "कल मिलते हैं 👍", "unread": 1}]
        self.assertIn("राहुल says: 'कल मिलते हैं 👍'", wa.build_summary(msgs, "today"))


class FakeManager:
    def __init__(self):
        self.sent = []

    def find_recipient(self, recipient):
        if recipient.lower() == "rahul":
            return {"ok": True, "recipient": "Rahul Sharma", "target": "Rahul Sharma"}
        raise WhatsAppError("not_found", f"I couldn't find '{recipient}'.")

    def send_message(self, target, text):
        self.sent.append((target, text))
        return {"ok": True, "status": "sent", "recipient": target, "message": "sent"}


class SendToolTest(unittest.TestCase):
    def setUp(self):
        self.fake = FakeManager()
        self._old = wa._manager
        wa._manager = self.fake
        wa._pending.clear()

    def tearDown(self):
        wa._manager = self._old
        wa._pending.clear()

    def test_asks_first_then_sends_with_token(self):
        args = {"recipient": "Rahul", "message": "मैं 5 बजे आऊँगा 👍"}
        first = wa.send_whatsapp_tool(args)
        self.assertTrue(first["needs_confirmation"])
        self.assertEqual(first["recipient"], "Rahul Sharma")
        self.assertEqual(self.fake.sent, [])

        done = wa.send_whatsapp_tool(dict(args, confirm_token=first["confirm_token"]))
        self.assertTrue(done["ok"])
        self.assertEqual(self.fake.sent, [("Rahul Sharma", "मैं 5 बजे आऊँगा 👍")])

    def test_changed_text_needs_new_confirmation(self):
        first = wa.send_whatsapp_tool({"recipient": "Rahul", "message": "5 baje"})
        again = wa.send_whatsapp_tool({"recipient": "Rahul", "message": "6 baje",
                                       "confirm_token": first["confirm_token"]})
        self.assertTrue(again["needs_confirmation"])
        self.assertEqual(self.fake.sent, [])

    def test_token_is_single_use(self):
        args = {"recipient": "Rahul", "message": "hi"}
        token = wa.send_whatsapp_tool(args)["confirm_token"]
        wa.send_whatsapp_tool(dict(args, confirm_token=token))
        repeat = wa.send_whatsapp_tool(dict(args, confirm_token=token))
        self.assertTrue(repeat["needs_confirmation"])
        self.assertEqual(len(self.fake.sent), 1)

    def test_made_up_token_does_not_send(self):
        result = wa.send_whatsapp_tool({"recipient": "Rahul", "message": "hi",
                                        "confirm_token": "deadbeef"})
        self.assertTrue(result["needs_confirmation"])
        self.assertEqual(self.fake.sent, [])

    def test_unknown_recipient_and_empty_message(self):
        self.assertEqual(wa.send_whatsapp_tool({"recipient": "Alex", "message": "hi"})["status"],
                         "not_found")
        self.assertEqual(wa.send_whatsapp_tool({"recipient": "Rahul", "message": " "})["status"],
                         "empty_message")


if __name__ == "__main__":
    unittest.main()
