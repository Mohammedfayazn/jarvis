"""window_manager ki matching, parsing aur unsaved detection - bina asli
windows chhue. Chalao:  python -m unittest discover tests
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import window_manager as wm  # noqa: E402
from window_manager import WindowInfo  # noqa: E402


def win(hwnd, title, process):
    return WindowInfo(hwnd=hwnd, title=title, process=process, pid=hwnd)


DESKTOP = [
    win(1, "Solution1 - Microsoft Visual Studio", "devenv.exe"),
    win(2, "main.py - jarvis - Visual Studio Code", "code.exe"),
    win(3, "Budget 2026.xlsx - Excel", "excel.exe"),
    win(4, "Inbox - Google Chrome", "chrome.exe"),
    win(5, "report.pdf - Google Chrome", "chrome.exe"),
    win(6, "manual.pdf - Adobe Acrobat Reader", "acrord32.exe"),
    win(7, "Inbox - me@example.com - Outlook", "outlook.exe"),
    win(8, "Chat | Microsoft Teams", "ms-teams.exe"),
    win(9, "Visual Studio Installer", "setup.exe"),
]


def matched(name):
    return sorted(w.hwnd for w in wm.find_windows(name, DESKTOP)[1])


class ResolveAppTest(unittest.TestCase):
    def test_visual_studio_is_not_vs_code(self):
        self.assertEqual(wm.resolve_app("visual studio").key, "visualstudio")
        self.assertEqual(wm.resolve_app("Microsoft Visual Studio").key, "visualstudio")
        for name in ("vscode", "VS Code", "visual studio code", "code"):
            self.assertEqual(wm.resolve_app(name).key, "vscode", name)

    def test_filler_words_and_prefixes(self):
        self.assertEqual(wm.resolve_app("the Excel window").key, "excel")
        self.assertEqual(wm.resolve_app("Microsoft Outlook").key, "outlook")
        self.assertEqual(wm.resolve_app("ms teams").key, "teams")

    def test_typos(self):
        self.assertEqual(wm.resolve_app("outlok").key, "outlook")
        self.assertEqual(wm.resolve_app("visul studio").key, "visualstudio")

    def test_unknown(self):
        self.assertIsNone(wm.resolve_app("budget report"))
        self.assertIsNone(wm.resolve_app(""))


class FindWindowsTest(unittest.TestCase):
    def test_close_visual_studio_matches_only_visual_studio(self):
        self.assertEqual(matched("visual studio"), [1])

    def test_vscode(self):
        self.assertEqual(matched("vscode"), [2])

    def test_every_chrome_window(self):
        self.assertEqual(matched("chrome"), [4, 5])
        self.assertEqual(matched("browser"), [4, 5])

    def test_pdf_never_closes_the_browser_holding_it(self):
        # report.pdf is a Chrome tab; closing "PDF" must not take Chrome down
        self.assertEqual(matched("pdf"), [6])

    def test_outlook_and_teams(self):
        self.assertEqual(matched("outlook"), [7])
        self.assertEqual(matched("teams"), [8])

    def test_fuzzy_title(self):
        self.assertEqual(matched("budget"), [3])
        self.assertEqual(matched("budgt"), [3])

    def test_no_match(self):
        self.assertEqual(matched("spotify"), [])       # known app, not open
        self.assertEqual(matched("zzqx flurble"), [])  # nothing like it

    def test_labels(self):
        app, found = wm.find_windows("budget", DESKTOP)
        self.assertEqual(wm._label(app, found, "budget"), "Excel")
        app, found = wm.find_windows("visual studio", DESKTOP)
        self.assertEqual(wm._label(app, found, "visual studio"), "Visual Studio")


class ParseCommandTest(unittest.TestCase):
    CASES = {
        "close chrome": ("close", "chrome"),
        "close excel": ("close", "excel"),
        "Close Outlook": ("close", "outlook"),
        "close visual studio": ("close", "visual studio"),
        "Jarvis, close Excel please": ("close", "excel"),
        "close the current browser tab": ("close", "the current browser tab"),
        "switch to vscode": ("focus", "vscode"),
        "Switch to Outlook": ("focus", "outlook"),
        "Bring Chrome to front": ("focus", "chrome"),
        "Activate VS Code": ("focus", "vs code"),
        "minimize teams": ("minimize", "teams"),
        "maximize browser": ("maximize", "browser"),
        "close all browser windows": ("close_all", "browser"),
        "close all outlook windows": ("close_all", "outlook"),
        "close all excel windows": ("close_all", "excel"),
        "close all windows except current": ("close_except", ""),
        "close everything except this": ("close_except", ""),
        "list windows": ("list", ""),
        "list": ("list", ""),
    }

    def test_supported_commands(self):
        for text, expected in self.CASES.items():
            self.assertEqual(wm.parse_command(text), expected, text)

    def test_unknown(self):
        self.assertIsNone(wm.parse_command("dance for me"))


class UnsavedTest(unittest.TestCase):
    def test_title_markers(self):
        dirty = [
            "● main.py - jarvis - Visual Studio Code",
            "*notes.txt - Notepad",
            "notes.txt * - Notepad++",
            "Untitled (Unsaved) - Editor",
        ]
        clean = [
            "main.py - jarvis - Visual Studio Code",
            "notes.txt - Notepad",
            "Budget 2026.xlsx - Excel",
            "C++ Primer - Chrome",
        ]
        for title in dirty:
            self.assertTrue(wm._is_unsaved(win(1, title, "x.exe"), {}), title)
        for title in clean:
            self.assertFalse(wm._is_unsaved(win(1, title, "x.exe"), {}), title)

    def test_office_names_from_com(self):
        excel = win(3, "Budget 2026.xlsx - Excel", "excel.exe")
        self.assertTrue(wm._is_unsaved(excel, {"excel.exe": ["Budget 2026.xlsx"]}))
        self.assertFalse(wm._is_unsaved(excel, {"excel.exe": ["Other.xlsx"]}))


class CurrentTabTest(unittest.TestCase):
    def test_current_tab_phrases(self):
        for phrase in ("current browser tab", "current tab", "tab",
                       "browser tab", "this tab", "active tab"):
            self.assertTrue(wm._CURRENT_TAB.match(phrase), phrase)
        self.assertFalse(wm._CURRENT_TAB.match("tableau"))


class HelpersTest(unittest.TestCase):
    def test_count_words(self):
        self.assertEqual(wm._count(3), "Three")
        self.assertEqual(wm._count(12), "12")

    def test_token_ignores_order_but_not_membership(self):
        a, b, c = DESKTOP[0], DESKTOP[1], DESKTOP[2]
        self.assertEqual(wm._token("x", [a, b]), wm._token("x", [b, a]))
        self.assertNotEqual(wm._token("x", [a, b]), wm._token("x", [a, c]))
        self.assertNotEqual(wm._token("x", [a]), wm._token("y", [a]))


if __name__ == "__main__":
    unittest.main()
