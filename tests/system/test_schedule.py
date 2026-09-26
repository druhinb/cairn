"""The launchd job: the rendered plist, reading it back, and the launchctl calls.

subprocess.run and the LaunchAgents path are replaced, so no test loads a real job.
"""
import contextlib
import io
import os
import plistlib
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from fastapi import FastAPI
from fastapi.testclient import TestClient
from helpers import app_client, temp_home

from cairn import cli
from cairn.system import schedule
from cairn.server import system

PROGRAM = "/Applications/Cairn/bin/cairn"


class FakeLaunchctl:
    """Records every command; `launchctl list` shows the job once it is loaded."""

    def __init__(self):
        self.calls = []
        self.loaded = False
        self.listed = set()  # labels of other loaded jobs
        self.pids = {}  # label -> pid of its running process
        self.fail = {}  # launchctl verb -> stderr it fails with

    def __call__(self, cmd, **kwargs):
        self.calls.append(cmd)
        verb = cmd[1]
        if verb in self.fail:
            return subprocess.CompletedProcess(cmd, 1, "", self.fail[verb])
        if verb == "load" and Path(cmd[2]).stem == schedule.LABEL:
            self.loaded = True
        elif verb == "load":
            self.listed.add(Path(cmd[2]).stem)
        elif verb == "unload" and Path(cmd[2]).stem == schedule.LABEL:
            self.loaded = False
        elif verb == "unload":
            self.listed.discard(Path(cmd[2]).stem)
        listing = "PID\tStatus\tLabel\n-\t0\tcom.apple.other\n"
        if self.loaded:
            listing += f"-\t0\t{schedule.LABEL}\n"
        listing += "".join(f"{self.pids.get(label, '-')}\t0\t{label}\n"
                           for label in sorted(self.listed))
        return subprocess.CompletedProcess(cmd, 0, listing if verb == "list" else "", "")


@unittest.skipIf(sys.platform == "win32", "launchd is macOS only")
class ScheduleTestCase(unittest.TestCase):
    def setUp(self):
        self.home = self.enterContext(temp_home())
        agents = Path(self.enterContext(tempfile.TemporaryDirectory())) / "LaunchAgents"
        self.plist = agents / f"{schedule.LABEL}.plist"
        self.launchctl = FakeLaunchctl()
        self.patch(schedule, "plist_path", lambda: self.plist)
        self.patch(schedule.subprocess, "run", self.launchctl)
        self.patch(schedule, "program", lambda: Path(PROGRAM))

    def patch(self, module, name, value):
        self.addCleanup(setattr, module, name, getattr(module, name))
        setattr(module, name, value)

    def write_plist(self, job):
        self.plist.parent.mkdir(parents=True, exist_ok=True)
        self.plist.write_bytes(plistlib.dumps(job))

    def verbs(self):
        return [cmd[1:] for cmd in self.launchctl.calls if cmd[1] != "list"]


class RenderTest(ScheduleTestCase):
    def test_renders_the_program_time_path_and_logs(self):
        job = plistlib.loads(schedule.render(PROGRAM, 21, 5).encode())
        self.assertEqual(job["Label"], "app.cairn.daily")
        self.assertEqual(job["ProgramArguments"], [PROGRAM, "run"])
        self.assertEqual(job["StartCalendarInterval"], {"Hour": 21, "Minute": 5})
        self.assertFalse(job["RunAtLoad"])
        path = job["EnvironmentVariables"]["PATH"].split(":")
        self.assertEqual(path, ["/opt/homebrew/bin", "/usr/local/bin",
                                f"{Path.home()}/.local/bin", f"{Path.home()}/.claude/local",
                                "/usr/bin", "/bin"])
        self.assertEqual(job["StandardOutPath"], str(self.home / "launchd.out"))
        self.assertEqual(job["StandardErrorPath"], str(self.home / "launchd.err"))

    def test_defaults_to_seven_am(self):
        job = plistlib.loads(schedule.render(PROGRAM).encode())
        self.assertEqual(job["StartCalendarInterval"], {"Hour": 7, "Minute": 0})

    def test_propagates_cairn_home(self):
        job = plistlib.loads(schedule.render(PROGRAM).encode())
        self.assertEqual(job["EnvironmentVariables"]["CAIRN_HOME"], str(self.home))

    def test_omits_cairn_home_when_unset(self):
        self.addCleanup(os.environ.__setitem__, "CAIRN_HOME", str(self.home))
        del os.environ["CAIRN_HOME"]
        job = plistlib.loads(schedule.render(PROGRAM).encode())
        self.assertNotIn("CAIRN_HOME", job["EnvironmentVariables"])
        self.assertEqual(job["StandardOutPath"],
                         str(Path("~/.cairn/launchd.out").expanduser()))

    def test_escapes_xml_in_paths(self):
        job = plistlib.loads(schedule.render("/tmp/R&D <x>/cairn").encode())
        self.assertEqual(job["ProgramArguments"][0], "/tmp/R&D <x>/cairn")


class StatusTest(ScheduleTestCase):
    def test_not_installed(self):
        self.assertEqual(schedule.status(), {
            "installed": False, "loaded": False, "plist": str(self.plist),
            "program": None, "hour": None, "minute": None, "hourly": False, "error": ""})

    def test_reads_an_installed_plist(self):
        self.plist.parent.mkdir(parents=True)
        self.plist.write_text(schedule.render(PROGRAM, 6, 30), encoding="utf-8")
        self.launchctl.loaded = True
        self.assertEqual(schedule.status(), {
            "installed": True, "loaded": True, "plist": str(self.plist),
            "program": PROGRAM, "hour": 6, "minute": 30, "hourly": False, "error": ""})

    def test_calendar_interval_given_as_an_array_shows_the_first_time(self):
        self.write_plist({"Label": schedule.LABEL, "ProgramArguments": [PROGRAM, "run"],
                          "StartCalendarInterval": [{"Hour": 9, "Minute": 45},
                                                    {"Hour": 18, "Minute": 0}]})
        job = schedule.status()
        self.assertEqual((job["program"], job["hour"], job["minute"], job["error"]),
                         (PROGRAM, 9, 45, ""))

    def test_the_launchctl_listing_failing_is_reported(self):
        self.launchctl.fail["list"] = "launchctl is broken"
        job = schedule.status()
        self.assertFalse(job["loaded"])
        self.assertEqual(job["error"], "launchctl list failed: launchctl is broken")


class MalformedPlistTest(ScheduleTestCase):
    """Each malformed plist is reported by status(), the CLI, and the API alike."""

    SHAPES = {
        "truncated": schedule.render(PROGRAM)[:200].encode(),
        "top-level list": plistlib.dumps([PROGRAM, "run"]),
        "garbage": b"\x00\xffnot a plist at all",
        "wrong field types": plistlib.dumps({"ProgramArguments": [PROGRAM],
                                             "StartCalendarInterval": "7am"}),
    }

    def each_shape(self):
        self.plist.parent.mkdir(parents=True, exist_ok=True)
        for shape, data in self.SHAPES.items():
            with self.subTest(shape):
                self.plist.write_bytes(data)
                yield

    def assert_reported(self, job):
        self.assertEqual({k: job[k] for k in ("installed", "loaded", "plist", "program",
                                              "hour", "minute")},
                         {"installed": True, "loaded": False, "plist": str(self.plist),
                          "program": None, "hour": None, "minute": None})
        self.assertTrue(job["error"].startswith(f"cannot read {self.plist}: "))
        self.assertNotIn("\n", job["error"])

    def test_status(self):
        for _ in self.each_shape():
            self.assert_reported(schedule.status())

    def test_cli_status(self):
        for _ in self.each_shape():
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = cli.commands.cmd_schedule(SimpleNamespace(action="status"))
            self.assertEqual(code, 0)
            self.assertIn("cannot read", out.getvalue())

    def test_api_status(self):
        client = app_client(self)
        for _ in self.each_shape():
            response = client.get("/api/schedule")
            self.assertEqual(response.status_code, 200)
            self.assert_reported(response.json())


class InstallTest(ScheduleTestCase):
    def test_writes_the_plists_and_reloads_them(self):
        result = schedule.install(8, 15)
        check = str(schedule.check_plist_path())
        self.assertEqual(self.verbs(), [["unload", str(self.plist)], ["load", str(self.plist)],
                                        ["unload", check], ["load", check]])
        self.assertEqual(result, {
            "installed": True, "loaded": True, "plist": str(self.plist),
            "program": PROGRAM, "hour": 8, "minute": 15, "hourly": True, "error": ""})
        self.assertTrue(self.home.is_dir())

    def test_the_hourly_check_runs_cairn_check_every_hour(self):
        schedule.install()
        with schedule.check_plist_path().open("rb") as f:
            job = plistlib.load(f)
        self.assertEqual((job["Label"], job["ProgramArguments"], job["StartInterval"],
                          job["Umask"], job["RunAtLoad"]),
                         (schedule.CHECK_LABEL, [PROGRAM, "check"], 3600, 0o077, False))
        self.assertEqual(job["EnvironmentVariables"]["CAIRN_HOME"], os.environ["CAIRN_HOME"])
        self.assertEqual(job["StandardErrorPath"], str(self.home / "check.err"))

    def test_a_failed_unload_is_ignored(self):
        self.launchctl.fail["unload"] = "Could not find specified service"
        self.assertTrue(schedule.install()["loaded"])

    def test_a_failed_load_surfaces_as_one_line(self):
        self.launchctl.fail["load"] = "noise\nLoad failed: 5: Input/output error"
        with self.assertRaises(schedule.ScheduleError) as caught:
            schedule.install()
        self.assertEqual(str(caught.exception),
                         "launchctl load failed: Load failed: 5: Input/output error")

    def test_a_hung_launchctl_surfaces_as_one_line(self):
        def hang(cmd, **kwargs):
            self.assertEqual(kwargs["timeout"], schedule.TIMEOUT)
            raise subprocess.TimeoutExpired(cmd, kwargs["timeout"])

        self.patch(schedule.subprocess, "run", hang)
        with self.assertRaisesRegex(schedule.ScheduleError,
                                    "^launchctl load failed: .* timed out"):
            schedule.install()


class RemoveTest(ScheduleTestCase):
    def test_unloads_and_deletes_both_jobs(self):
        schedule.install()
        self.launchctl.calls.clear()
        result = schedule.remove()
        check = schedule.check_plist_path()
        self.assertEqual(self.verbs(), [["unload", str(self.plist)], ["unload", str(check)]])
        self.assertFalse(self.plist.exists() or check.exists())
        self.assertEqual((result["installed"], result["loaded"], result["hourly"]),
                         (False, False, False))

    def test_a_loaded_job_without_its_file_is_removed_by_label(self):
        self.launchctl.fail["remove"] = "Could not find service"
        self.assertFalse(schedule.remove()["installed"])
        self.assertEqual(self.verbs(), [["remove", schedule.LABEL],
                                        ["remove", schedule.CHECK_LABEL]])


class AutostartTest(ScheduleTestCase):
    def setUp(self):
        super().setUp()
        self.ui_plist = self.plist.parent / f"{schedule.UI_LABEL}.plist"
        self.patch(schedule, "autostart_plist_path", lambda: self.ui_plist)

    def status(self, **fields):
        return {"installed": False, "loaded": False, "plist": str(self.ui_plist),
                "program": None, "error": "", **fields}

    def test_renders_a_login_item_that_opens_the_app(self):
        job = plistlib.loads(schedule.render_autostart(PROGRAM).encode())
        self.assertEqual(job["Label"], "app.cairn.autostart")
        self.assertEqual(job["ProgramArguments"], [PROGRAM, "ui"])
        self.assertIs(job["RunAtLoad"], True)
        self.assertIs(job["KeepAlive"], False)
        self.assertEqual(job["EnvironmentVariables"]["CAIRN_HOME"], str(self.home))
        self.assertIn("/opt/homebrew/bin", job["EnvironmentVariables"]["PATH"])
        self.assertEqual(job["StandardErrorPath"], str(self.home / "ui.err"))

    def test_install_writes_the_plist_for_the_next_login_without_loading_it(self):
        self.assertEqual(schedule.autostart_install(),
                         self.status(installed=True, program=PROGRAM))
        self.assertEqual(self.verbs(), [])
        self.assertFalse(self.plist.exists())

    def test_status_reports_a_loaded_item(self):
        schedule.autostart_install()
        self.launchctl.listed.add(schedule.UI_LABEL)
        self.assertEqual(schedule.autostart_status(),
                         self.status(installed=True, loaded=True, program=PROGRAM))

    def test_remove_unloads_and_deletes_and_leaves_the_daily_job_alone(self):
        schedule.install()
        schedule.autostart_install()
        self.launchctl.calls.clear()
        self.assertEqual(schedule.autostart_remove(), self.status())
        self.assertEqual(self.verbs(), [["unload", str(self.ui_plist)]])
        self.assertTrue(self.plist.exists())
        self.assertTrue(schedule.status()["loaded"])

    def test_a_loaded_item_without_its_file_is_removed_by_label(self):
        self.assertEqual(schedule.autostart_remove(), self.status())
        self.assertEqual(self.verbs(), [["remove", schedule.UI_LABEL]])

    def test_remove_from_the_app_the_item_opened_leaves_it_running(self):
        schedule.autostart_install()
        self.launchctl.listed.add(schedule.UI_LABEL)
        self.launchctl.pids[schedule.UI_LABEL] = os.getpid()
        self.assertEqual(schedule.autostart_remove(), self.status(loaded=True))
        self.assertEqual(self.verbs(), [])
        self.assertFalse(self.ui_plist.exists())

    def test_remove_from_an_app_launchd_marks_as_the_items_job(self):
        schedule.autostart_install()
        self.launchctl.listed.add(schedule.UI_LABEL)
        with mock.patch.dict(os.environ, XPC_SERVICE_NAME=schedule.UI_LABEL):
            schedule.autostart_remove()
        self.assertEqual(self.verbs(), [])
        self.assertFalse(self.ui_plist.exists())

    def test_remove_unloads_an_item_running_another_process(self):
        schedule.autostart_install()
        self.launchctl.listed.add(schedule.UI_LABEL)
        self.launchctl.pids[schedule.UI_LABEL] = os.getpid() + 1
        with mock.patch.dict(os.environ, XPC_SERVICE_NAME="0"):
            self.assertEqual(schedule.autostart_remove(), self.status())
        self.assertEqual(self.verbs(), [["unload", str(self.ui_plist)]])

    def test_a_malformed_plist_is_reported(self):
        self.ui_plist.parent.mkdir(parents=True, exist_ok=True)
        self.ui_plist.write_bytes(b"\x00 not a plist")
        job = schedule.autostart_status()
        self.assertTrue(job["installed"])
        self.assertTrue(job["error"].startswith(f"cannot read {self.ui_plist}: "))

    def test_cli(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cli.commands.cmd_autostart(cli.parser.build_parser().parse_args(["autostart", "install"]))
        self.assertEqual(code, 0)
        self.assertIn("The app opens at your next login.", out.getvalue())
        self.assertTrue(self.ui_plist.exists())

    def test_api(self):
        app = FastAPI()
        app.include_router(system.router)
        client = self.enterContext(TestClient(app))
        self.assertEqual(client.get("/api/autostart").json(), self.status())
        self.assertEqual(client.post("/api/autostart/install").json(),
                         self.status(installed=True, program=PROGRAM))
        self.assertEqual(client.post("/api/autostart/remove").json(), self.status())
        self.launchctl.fail["remove"] = "Operation not permitted"
        self.launchctl.fail["list"] = "launchctl is broken"

        def refuse():
            raise schedule.ScheduleError("no cairn executable found")

        self.patch(schedule, "program", refuse)
        response = client.post("/api/autostart/install")
        self.assertEqual((response.status_code, response.json()),
                         (500, {"detail": "no cairn executable found"}))


@unittest.skipIf(sys.platform == "win32", "launchd is macOS only")
class ProgramTest(unittest.TestCase):
    def setUp(self):
        self.addCleanup(setattr, schedule.sys, "argv", schedule.sys.argv)
        self.addCleanup(setattr, schedule.shutil, "which", schedule.shutil.which)

    def test_the_app_bundle_runs_its_own_executable(self):
        self.addCleanup(setattr, schedule.sys, "executable", schedule.sys.executable)
        self.addCleanup(delattr, schedule.sys, "frozen")
        schedule.sys.frozen = True
        schedule.sys.executable = "/Applications/Cairn.app/Contents/MacOS/Cairn"
        self.assertEqual(schedule.program(), Path(schedule.sys.executable))

    def test_a_translocated_app_is_refused_with_the_fix(self):
        self.addCleanup(setattr, schedule.sys, "executable", schedule.sys.executable)
        self.addCleanup(delattr, schedule.sys, "frozen")
        schedule.sys.frozen = True
        schedule.sys.executable = ("/private/var/folders/x/AppTranslocation/0A1B/d/"
                                   "Cairn.app/Contents/MacOS/Cairn")
        fix = "move Cairn.app to /Applications and open it once, then try again"
        with self.assertRaisesRegex(schedule.ScheduleError, fix):
            schedule.program()
        for action in ("schedule", "autostart"):
            with self.subTest(action), temp_home():
                err = io.StringIO()
                with contextlib.redirect_stderr(err), contextlib.redirect_stdout(err):
                    code = cli.parser.build_parser().parse_args([action, "install"]).func(
                        cli.parser.build_parser().parse_args([action, "install"]))
                self.assertEqual(code, 1)
                self.assertIn(fix, err.getvalue())

    def test_uses_the_running_executable(self):
        with tempfile.TemporaryDirectory() as bin_dir:
            exe = Path(bin_dir) / "cairn"
            exe.touch()
            schedule.sys.argv = [str(exe), "schedule", "install"]
            self.assertEqual(schedule.program(), exe.resolve())

    def test_a_running_executable_that_no_longer_exists_falls_back_to_path(self):
        schedule.sys.argv = ["/nowhere/bin/cairn", "schedule", "install"]
        schedule.shutil.which = lambda name: f"/opt/bin/{name}"
        self.assertEqual(schedule.program(), Path("/opt/bin/cairn"))

    def test_falls_back_to_path(self):
        schedule.sys.argv = ["/usr/bin/python3", "-m", "unittest"]
        schedule.shutil.which = lambda name: f"/opt/bin/{name}"
        self.assertEqual(schedule.program(), Path("/opt/bin/cairn"))

    def test_no_executable_says_how_to_install(self):
        schedule.sys.argv = ["/usr/bin/python3", "-m", "unittest"]
        schedule.shutil.which = lambda name: None
        with self.assertRaisesRegex(schedule.ScheduleError, "uv tool install"):
            schedule.program()


if __name__ == "__main__":
    unittest.main()
