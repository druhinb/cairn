"""The Windows schedule: the Task Scheduler task for the daily run and the Run key
entry for login. Most tests fake schtasks and the registry and run anywhere; the
ones marked for Windows register a task and a Run entry of their own for real and
remove them again."""
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

from helpers import temp_home

from cairn.system import schedule_windows as windows
from cairn.system.schedule import ScheduleError

WINDOWS = sys.platform == "win32"


class FakeSchtasks:
    """Answers schtasks like Task Scheduler with one task registered or none."""

    def __init__(self):
        self.registered = False
        self.calls = []
        self.fail_create = None

    def __call__(self, cmd, **kwargs):
        self.calls.append(cmd[1:])
        verb = cmd[1]
        if verb == "/Query":
            return subprocess.CompletedProcess(cmd, 0 if self.registered else 1, "", "")
        if verb == "/Create" and self.fail_create:
            return subprocess.CompletedProcess(cmd, 1, "", self.fail_create)
        self.registered = verb == "/Create"
        return subprocess.CompletedProcess(cmd, 0, "SUCCESS", "")


class FakeKey:
    def __init__(self, values):
        self.values = values

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeWinreg:
    """The slice of winreg the Run entry uses, over one dict."""
    HKEY_CURRENT_USER, KEY_SET_VALUE, REG_SZ = "HKCU", 2, 1

    def __init__(self):
        self.values = {}

    def CreateKey(self, root, path):  # noqa: N802 - winreg's names
        return FakeKey(self.values)

    def OpenKey(self, root, path, reserved=0, access=0):  # noqa: N802
        return FakeKey(self.values)

    def SetValueEx(self, key, name, reserved, kind, value):  # noqa: N802
        key.values[name] = value

    def QueryValueEx(self, key, name):  # noqa: N802
        if name not in key.values:
            raise FileNotFoundError(2, "The system cannot find the file specified")
        return key.values[name], self.REG_SZ

    def DeleteValue(self, key, name):  # noqa: N802
        if name not in key.values:
            raise FileNotFoundError(2, "The system cannot find the file specified")
        del key.values[name]


class WindowsTestCase(unittest.TestCase):
    def setUp(self):
        self.home = self.enterContext(temp_home())
        scripts = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.pythonw = scripts / "pythonw.exe"
        self.pythonw.touch()
        self.enterContext(mock.patch.object(windows.sys, "executable", str(scripts / "python.exe")))


class TaskTest(WindowsTestCase):
    def setUp(self):
        super().setUp()
        self.schtasks = FakeSchtasks()
        self.enterContext(mock.patch.object(windows.subprocess, "run", self.schtasks))

    def test_install_registers_the_definition_it_keeps(self):
        job = windows.install(6, 5)
        self.assertEqual(self.schtasks.calls[0],
                         ["/Create", "/TN", windows.TASK, "/XML", str(windows.task_file()), "/F"])
        self.assertTrue(windows.task_file().read_bytes().startswith(b"\xff\xfe"))
        self.assertEqual(job, {"installed": True, "loaded": True, "plist": windows.TASK,
                               "program": str(self.pythonw), "hour": 6, "minute": 5,
                               "error": ""})

    def test_the_task_runs_cairn_from_the_data_home_and_catches_up_after_sleep(self):
        text = windows.render(r"C:\Tools & more\pythonw.exe", 7, 0)
        self.assertIn(r"<Command>C:\Tools &amp; more\pythonw.exe</Command>", text)
        self.assertIn("<Arguments>-m cairn.cli run</Arguments>", text)
        self.assertIn(f"<WorkingDirectory>{self.home}</WorkingDirectory>", text)
        self.assertIn("<StartWhenAvailable>true</StartWhenAvailable>", text)

    def test_a_refused_task_surfaces_as_one_line(self):
        self.schtasks.fail_create = "ERROR: Access is denied.\r\n"
        with self.assertRaisesRegex(ScheduleError,
                                    r"^schtasks /Create failed: ERROR: Access is denied\.$"):
            windows.install()

    def test_remove_unregisters_and_deletes(self):
        windows.install()
        job = windows.remove()
        self.assertIn(["/Delete", "/TN", windows.TASK, "/F"], self.schtasks.calls)
        self.assertFalse(windows.task_file().exists())
        self.assertEqual((job["installed"], job["loaded"]), (False, False))

    def test_remove_without_a_task_deletes_nothing_in_task_scheduler(self):
        windows.remove()
        self.assertNotIn("/Delete", [call[0] for call in self.schtasks.calls])

    def test_status_without_schtasks_says_why(self):
        missing = FileNotFoundError("schtasks")
        with mock.patch.object(windows.subprocess, "run", side_effect=missing):
            job = windows.status()
        self.assertFalse(job["loaded"])
        self.assertTrue(job["error"].startswith("schtasks /Query failed"))

    def test_a_malformed_definition_is_reported(self):
        windows.task_file().write_text("not xml", encoding="utf-8")
        job = windows.status()
        self.assertTrue(job["installed"])
        self.assertIn("cannot read", job["error"])

    def test_no_pythonw_says_how_to_reinstall(self):
        self.pythonw.unlink()
        with self.assertRaisesRegex(ScheduleError, "reinstall Cairn"):
            windows.install()


class RunEntryTest(WindowsTestCase):
    def setUp(self):
        super().setUp()
        self.winreg = FakeWinreg()
        self.enterContext(mock.patch.dict(sys.modules, {"winreg": self.winreg}))

    def test_install_adds_an_entry_that_opens_the_app(self):
        item = windows.autostart_install()
        self.assertEqual(self.winreg.values, {"Cairn": f'"{self.pythonw}" -m cairn.cli ui'})
        self.assertEqual((item["installed"], item["loaded"], item["program"], item["error"]),
                         (True, True, str(self.pythonw), ""))

    def test_remove_deletes_the_entry_and_is_quiet_without_one(self):
        windows.autostart_install()
        self.assertFalse(windows.autostart_remove()["installed"])
        self.assertEqual(self.winreg.values, {})
        self.assertFalse(windows.autostart_remove()["installed"])


@unittest.skipUnless(WINDOWS, "needs Task Scheduler and the registry")
class RealWindowsTest(unittest.TestCase):
    def setUp(self):
        self.enterContext(temp_home())
        name = f"Cairn test {uuid.uuid4().hex[:8]}"
        self.enterContext(mock.patch.object(windows, "TASK", name))
        self.enterContext(mock.patch.object(windows, "RUN_VALUE", name))

    def test_the_task_registers_reads_back_and_goes(self):
        self.addCleanup(windows.remove)
        job = windows.install(6, 5)
        self.assertEqual((job["loaded"], job["hour"], job["minute"], job["error"]),
                         (True, 6, 5, ""))
        self.assertTrue(Path(job["program"]).exists())
        self.assertFalse(windows.remove()["loaded"])

    def test_the_run_entry_is_added_and_removed(self):
        self.addCleanup(windows.autostart_remove)
        self.assertTrue(windows.autostart_install()["installed"])
        self.assertFalse(windows.autostart_remove()["installed"])
