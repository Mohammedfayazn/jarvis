"""system_control ki path safety, confirmation aur routing - bina asli
volume, brightness ya power ko chhue. Chalao:

    python -m unittest discover tests
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import system_control as sc  # noqa: E402
from system_control import SystemControlError, SystemController  # noqa: E402


class PercentTest(unittest.TestCase):
    def test_clamped_and_rounded(self):
        self.assertEqual(sc._percent(30), 30)
        self.assertEqual(sc._percent("45"), 45)
        self.assertEqual(sc._percent(30.6), 31)
        self.assertEqual(sc._percent(-10), 0)
        self.assertEqual(sc._percent(180), 100)

    def test_nonsense(self):
        with self.assertRaises(SystemControlError) as ctx:
            sc._percent("loud")
        self.assertEqual(ctx.exception.status, "failed")


class PathTest(unittest.TestCase):
    def setUp(self):
        self.pc = SystemController()
        self.tmp = Path(tempfile.mkdtemp())

    def test_relative_and_variables_resolve(self):
        self.assertTrue(self.pc._resolve("subfolder").is_absolute())
        self.assertEqual(self.pc._resolve("%TEMP%").resolve(),
                         Path(os.environ["TEMP"]).resolve())

    def test_missing_drive_says_which_drives_exist(self):
        with self.assertRaises(SystemControlError) as ctx:
            self.pc._resolve(r"Z:\Projects\Logs")
        self.assertEqual(ctx.exception.status, "no_drive")
        self.assertIn("C:", ctx.exception.message)

    def test_wildcards_and_empty(self):
        for bad in (r"D:\*.txt", "", "   "):
            with self.assertRaises(SystemControlError) as ctx:
                self.pc._resolve(bad)
            self.assertEqual(ctx.exception.status, "bad_path", bad)

    def test_must_exist(self):
        with self.assertRaises(SystemControlError) as ctx:
            self.pc._resolve(self.tmp / "nothing-here", must_exist=True)
        self.assertEqual(ctx.exception.status, "not_found")


class GuardTest(unittest.TestCase):
    """The places a spoken mistake must never reach."""

    def setUp(self):
        self.pc = SystemController()

    def refused(self, path):
        with self.assertRaises(SystemControlError) as ctx:
            self.pc._guard(Path(path).resolve(), "delete")
        return ctx.exception

    def test_drive_root(self):
        self.assertEqual(self.refused("C:\\").status, "protected")

    def test_windows_and_program_files_inside_too(self):
        self.assertEqual(self.refused(os.environ["SystemRoot"]).status, "protected")
        self.assertEqual(self.refused(Path(os.environ["SystemRoot"]) / "System32").status,
                         "protected")

    def test_jarvis_own_data_and_code(self):
        import app_paths
        self.assertEqual(self.refused(app_paths.home()).status, "protected")
        self.assertEqual(self.refused(app_paths.home() / "speech_coach").status, "protected")
        self.assertEqual(self.refused(app_paths.CODE_DIR / "main.py").status, "protected")

    def test_user_root_is_kept_but_files_inside_are_allowed(self):
        self.assertEqual(self.refused(Path.home()).status, "protected")
        self.pc._guard(Path.home() / "Documents" / "notes.txt", "delete")   # no raise


class FileOperationTest(unittest.TestCase):
    def setUp(self):
        self.pc = SystemController()
        self.tmp = Path(tempfile.mkdtemp())

    def test_create_is_idempotent(self):
        target = self.tmp / "Projects" / "Jarvis_Logs"
        first = self.pc.create_folder(str(target))
        self.assertTrue(first["created"])
        self.assertTrue(target.is_dir())
        self.assertFalse(self.pc.create_folder(str(target))["created"])

    def test_create_over_a_file(self):
        f = self.tmp / "file.txt"
        f.write_text("x", encoding="utf-8")
        with self.assertRaises(SystemControlError) as ctx:
            self.pc.create_folder(str(f))
        self.assertEqual(ctx.exception.status, "bad_path")

    def test_list_counts_and_names(self):
        (self.tmp / "a").mkdir()
        (self.tmp / "b.txt").write_text("hello", encoding="utf-8")
        result = self.pc.list_folder(str(self.tmp))
        self.assertEqual((result["folders"], result["files"]), (1, 1))
        self.assertEqual([e["name"] for e in result["entries"]], ["a", "b.txt"])

    def test_move_refuses_to_overwrite(self):
        src, dst = self.tmp / "one.txt", self.tmp / "two.txt"
        src.write_text("1", encoding="utf-8")
        dst.write_text("2", encoding="utf-8")
        with self.assertRaises(SystemControlError):
            self.pc.move(str(src), str(dst))
        self.assertEqual(dst.read_text(encoding="utf-8"), "2")

    def test_move_into_a_folder_keeps_the_name(self):
        src = self.tmp / "note.txt"
        src.write_text("1", encoding="utf-8")
        (self.tmp / "sub").mkdir()
        result = self.pc.move(str(src), str(self.tmp / "sub"))
        self.assertTrue((self.tmp / "sub" / "note.txt").is_file())
        self.assertEqual(Path(result["path"]).name, "note.txt")

    def test_delete_asks_first_and_the_token_is_specific(self):
        one = self.tmp / "one.txt"
        other = self.tmp / "other.txt"
        one.write_text("1", encoding="utf-8")
        other.write_text("2", encoding="utf-8")

        ask = self.pc.delete(str(one))
        self.assertTrue(ask["needs_confirmation"])
        self.assertTrue(one.exists())                       # nothing happened yet

        # a token from another file must not delete this one
        wrong = self.pc.delete(str(other))["confirm_token"]
        self.assertTrue(self.pc.delete(str(one), wrong)["needs_confirmation"])
        self.assertTrue(one.exists())

        with mock.patch("send2trash.send2trash") as trash:
            done = self.pc.delete(str(one), ask["confirm_token"])
        trash.assert_called_once_with(str(one.resolve()))
        self.assertTrue(done["ok"])


class PowerTest(unittest.TestCase):
    def setUp(self):
        self.pc = SystemController()

    def test_shutdown_asks_before_running_anything(self):
        with mock.patch.object(SystemController, "_run") as run:
            ask = self.pc.power("shutdown", seconds=60)
            run.assert_not_called()
        self.assertTrue(ask["needs_confirmation"])
        self.assertIn("60 seconds", ask["message"])

    def test_confirmed_shutdown_runs_the_command(self):
        ask = self.pc.power("shutdown", seconds=30)
        with mock.patch.object(SystemController, "_run") as run:
            done = self.pc.power("shutdown", seconds=30, confirm_token=ask["confirm_token"])
        run.assert_called_once_with(["shutdown", "/s", "/t", "30"], check=True)
        self.assertTrue(done["ok"])

    def test_a_token_for_another_delay_does_not_count(self):
        ask = self.pc.power("shutdown", seconds=30)
        with mock.patch.object(SystemController, "_run") as run:
            again = self.pc.power("shutdown", seconds=0, confirm_token=ask["confirm_token"])
            run.assert_not_called()
        self.assertTrue(again["needs_confirmation"])

    def test_restart_token_does_not_shut_down(self):
        restart = self.pc.power("restart", seconds=60)["confirm_token"]
        with mock.patch.object(SystemController, "_run") as run:
            self.pc.power("shutdown", seconds=60, confirm_token=restart)
            run.assert_not_called()

    def test_lock_and_sleep_need_no_confirmation(self):
        with mock.patch.object(SystemController, "_run") as run:
            self.assertTrue(self.pc.power("lock")["ok"])
            self.assertTrue(self.pc.power("sleep")["ok"])
        self.assertEqual(run.call_args_list[0].args[0],
                         ["rundll32.exe", "user32.dll,LockWorkStation"])
        self.assertEqual(run.call_args_list[1].args[0],
                         ["rundll32.exe", "powrprof.dll,SetSuspendState", "0,1,0"])

    def test_cancel(self):
        with mock.patch.object(SystemController, "_run") as run:
            run.return_value = mock.Mock(returncode=0)
            self.assertTrue(self.pc.power("cancel")["cancelled"])
            run.assert_called_once_with(["shutdown", "/a"])

    def test_unknown_action(self):
        with self.assertRaises(SystemControlError):
            self.pc.power("explode")


class MediaAndBatteryTest(unittest.TestCase):
    def setUp(self):
        self.pc = SystemController()

    def test_media_keys(self):
        with mock.patch("pyautogui.press") as press:
            for spoken, key in (("play", "playpause"), ("pause", "playpause"),
                                ("next", "nexttrack"), ("previous", "prevtrack")):
                self.pc.media(spoken)
                self.assertEqual(press.call_args.args[0], key, spoken)

    def test_unknown_media_action(self):
        with self.assertRaises(SystemControlError):
            self.pc.media("rewind")

    def test_battery_messages(self):
        from collections import namedtuple
        Case = namedtuple("sbattery", "percent secsleft power_plugged")
        with mock.patch("psutil.sensors_battery", return_value=Case(99.0, -2, True)):
            self.assertIn("99 percent and charging", self.pc.battery()["message"])
        with mock.patch("psutil.sensors_battery", return_value=Case(15.0, 1800, False)):
            message = self.pc.battery()["message"]
            self.assertIn("30 minutes left", message)
            self.assertIn("Running low", message)
        with mock.patch("psutil.sensors_battery", return_value=None):
            result = self.pc.battery()
            self.assertFalse(result["has_battery"])
            self.assertIn("no battery", result["message"])


class ForceCloseTest(unittest.TestCase):
    def setUp(self):
        self.pc = SystemController()

    def fake_processes(self, *names):
        procs = []
        for pid, name in enumerate(names, start=1000):
            proc = mock.Mock()
            proc.info = {"name": name, "pid": pid}
            procs.append(proc)
        return procs

    def test_windows_processes_are_refused(self):
        with mock.patch("psutil.process_iter", return_value=self.fake_processes("csrss.exe")):
            with self.assertRaises(SystemControlError) as ctx:
                self.pc.force_close_app("csrss")
        self.assertEqual(ctx.exception.status, "protected")

    def test_asks_before_killing(self):
        procs = self.fake_processes("notepad.exe", "notepad.exe")
        with mock.patch("psutil.process_iter", return_value=procs):
            ask = self.pc.force_close_app("notepad")
        self.assertTrue(ask["needs_confirmation"])
        self.assertEqual(ask["count"], 2)
        for proc in procs:
            proc.terminate.assert_not_called()

    def test_kills_after_confirmation(self):
        procs = self.fake_processes("notepad.exe")
        with mock.patch("psutil.process_iter", return_value=procs):
            token = self.pc.force_close_app("notepad")["confirm_token"]
            with mock.patch("psutil.wait_procs", return_value=(procs, [])):
                done = self.pc.force_close_app("notepad", token)
        procs[0].terminate.assert_called_once()
        self.assertTrue(done["ok"])

    def test_jarvis_never_closes_itself(self):
        proc = mock.Mock()
        proc.info = {"name": "python.exe", "pid": os.getpid()}
        with mock.patch("psutil.process_iter", return_value=[proc]):
            with self.assertRaises(SystemControlError) as ctx:
                self.pc.force_close_app("python")
        self.assertEqual(ctx.exception.status, "not_found")


class VoiceToolTest(unittest.TestCase):
    """The dispatcher main.py uses: every tool answers, never raises."""

    def setUp(self):
        self.pc = mock.Mock(spec=SystemController)
        patcher = mock.patch("system_control.controller", return_value=self.pc)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_volume_routing(self):
        sc.VOICE_TOOLS["control_volume"]({"action": "set", "percent": 30})
        self.pc.set_volume.assert_called_once_with(30)
        sc.VOICE_TOOLS["control_volume"]({"action": "up", "percent": 10})
        self.pc.change_volume.assert_called_once_with(10.0)
        sc.VOICE_TOOLS["control_volume"]({"action": "down", "percent": 10})
        self.assertEqual(self.pc.change_volume.call_args.args[0], -10.0)
        sc.VOICE_TOOLS["control_volume"]({"action": "down"})     # no number: a sane step
        self.assertEqual(self.pc.change_volume.call_args.args[0], -10.0)
        sc.VOICE_TOOLS["control_volume"]({})
        self.pc.volume_status.assert_called_once()

    def test_brightness_routing(self):
        sc.VOICE_TOOLS["control_brightness"]({"action": "set", "percent": 50})
        self.pc.set_brightness.assert_called_once_with(50, None)
        sc.VOICE_TOOLS["control_brightness"]({"action": "up", "percent": 20,
                                              "display": "Philips"})
        self.assertEqual(self.pc.change_brightness.call_args.args, (20.0, "Philips"))

    def test_files_routing(self):
        sc.VOICE_TOOLS["manage_files"]({"action": "create_folder", "path": r"D:\Data"})
        self.pc.create_folder.assert_called_once_with(r"D:\Data")
        sc.VOICE_TOOLS["manage_files"]({"action": "delete", "path": "x", "confirm_token": "t"})
        self.pc.delete.assert_called_once_with("x", "t")
        bad = sc.VOICE_TOOLS["manage_files"]({"action": "burn", "path": "x"})
        self.assertFalse(bad["ok"])

    def test_errors_become_results(self):
        self.pc.set_volume.side_effect = SystemControlError("failed", "no speakers")
        result = sc.VOICE_TOOLS["control_volume"]({"action": "set", "percent": 10})
        self.assertEqual((result["ok"], result["status"]), (False, "failed"))

    def test_unexpected_errors_are_caught_too(self):
        import logging
        self.pc.status.side_effect = RuntimeError("boom")
        with self.assertLogs("jarvis.system", level=logging.ERROR):
            result = sc.VOICE_TOOLS["system_status"]({})
        self.assertFalse(result["ok"])
        self.assertIn("RuntimeError", result["message"])


if __name__ == "__main__":
    unittest.main()
