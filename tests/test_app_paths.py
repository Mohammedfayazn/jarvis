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
