import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import app_paths


class AppPathsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.home = self.tmp / "home"
        self.src = self.tmp / "src"
        patcher = mock.patch.dict(os.environ, {"JARVIS_HOME": str(self.home)})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def legacy(self, component, name, text):
        path = self.src / component / "data" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def test_paths_follow_jarvis_home(self):
        self.assertEqual(app_paths.home(), self.home)
        self.assertEqual(app_paths.data_dir("memory"), self.home / "memory")
        self.assertEqual(app_paths.env_file(), self.home / ".env")

    def test_copies_legacy_data_once_and_keeps_originals(self):
        self.legacy("memory", "jarvis_memory.db", "old facts")
        self.legacy("memory", "jarvis_memory.db-wal", "wal")
        self.assertEqual(app_paths.migrate_legacy_data(self.src), ["memory"])
        self.assertEqual((self.home / "memory" / "jarvis_memory.db").read_text(), "old facts")
        self.assertTrue((self.home / "memory" / "jarvis_memory.db-wal").exists())
        self.assertTrue((self.src / "memory" / "data" / "jarvis_memory.db").exists())

    def test_never_overwrites_newer_data(self):
        self.legacy("islamic", "islamic.db", "old")
        target = self.home / "islamic" / "islamic.db"
        target.parent.mkdir(parents=True)
        target.write_text("new progress")
        self.assertEqual(app_paths.migrate_legacy_data(self.src), [])
        self.assertEqual(target.read_text(), "new progress")

    def test_nothing_to_copy(self):
        self.assertEqual(app_paths.migrate_legacy_data(self.src), [])


if __name__ == "__main__":
    unittest.main()


class RedirectGuardTest(unittest.TestCase):
    """A process whose AppData writes are redirected must not silently open
    Jarvis's live databases: that is how the profiles went missing once."""

    def test_warns_once_for_a_live_database(self):
        import logging
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "Jarvis"
            (home / "speech_coach").mkdir(parents=True)
            with mock.patch.dict(os.environ, {"JARVIS_HOME": str(home)}):
                app_paths._warned = False
                app_paths._REDIRECT = Path(tmp) / "Packages" / "App" / "LocalCache"
                db = home / "speech_coach" / "speech_coach.db"
                with self.assertLogs("jarvis.data", level=logging.WARNING) as logs:
                    self.assertIsNotNone(app_paths.warn_if_redirected(db))
                self.assertIn("redirected", logs.output[0])
                # only the first open shouts
                app_paths.warn_if_redirected(db)
                self.assertIsNone(app_paths.warn_if_redirected(":memory:"))
                self.assertIsNone(app_paths.warn_if_redirected(Path(tmp) / "elsewhere.db"))
            app_paths._REDIRECT = "unknown"
            app_paths._warned = False
