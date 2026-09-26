"""doctor.checks() and `cairn doctor`, with every binary and subprocess faked."""
import contextlib
import dataclasses
import datetime
import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from helpers import temp_home
from test_llm import KEY, llm_home

from cairn import cli, doctor, llm, schedule, secrets, settings

NAMES = ["python", "claude", "home", "schedule", "start at login", "pdftotext"]
HOME_FILES = ("config.toml", "profile.md")


class DoctorTest(unittest.TestCase):
    def setUp(self):
        self.home = self.enterContext(temp_home())
        agents = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.plist = agents / f"{schedule.LABEL}.plist"
        self.binaries = {"claude", "pdftotext"}
        self.claude_version = subprocess.CompletedProcess([], 0, "2.1.0 (Claude Code)", "")
        self.patch(doctor.shutil, "which",
                   lambda name, **kwargs: f"/bin/{name}" if name in self.binaries else None)
        self.patch(doctor.subprocess, "run", self.fake_run)
        self.patch(schedule, "plist_path", lambda: self.plist)
        self.patch(schedule, "autostart_plist_path", lambda: self.home / "absent-ui.plist")
        for name in HOME_FILES:
            (self.home / name).write_text("", encoding="utf-8")

    def patch(self, module, name, value):
        self.addCleanup(setattr, module, name, getattr(module, name))
        setattr(module, name, value)

    def fake_run(self, cmd, **kwargs):
        if cmd[0] == "/bin/claude":
            return self.claude_version
        if cmd[:2] == ["launchctl", "list"]:
            return subprocess.CompletedProcess(cmd, 0, "PID\tStatus\tLabel\n", "")
        if cmd[:2] == ["schtasks", "/Query"]:
            return subprocess.CompletedProcess(cmd, 1, "", "ERROR: no such task")
        raise AssertionError(f"unexpected command: {cmd}")

    def checks(self):
        return {c["name"]: c for c in doctor.checks()}

    def test_every_check_runs_and_passes_on_a_ready_machine(self):
        checks = doctor.checks()
        self.assertEqual([c["name"] for c in checks], NAMES)
        failed = [c for c in checks if not c["ok"]]
        self.assertEqual(failed, [])
        self.assertTrue(all(c["fix"] == "" for c in checks))

    def test_missing_claude(self):
        self.binaries.discard("claude")
        claude = self.checks()["claude"]
        self.assertFalse(claude["ok"])
        self.assertEqual(claude["detail"], "Claude Code isn't installed on this Mac")
        self.assertIn("claude.com/claude-code", claude["fix"])

    def test_claude_that_cannot_report_its_version(self):
        self.claude_version = subprocess.CompletedProcess([], 1, "", "not logged in")
        claude = self.checks()["claude"]
        self.assertFalse(claude["ok"])
        self.assertIn("not logged in", claude["detail"])

    def test_uninitialised_home(self):
        (self.home / "profile.md").unlink()
        home = self.checks()["home"]
        self.assertFalse(home["ok"])
        self.assertEqual(home["detail"], "Setup isn't finished")
        self.assertEqual(home["fix"], "finish setup in the app")

    def test_schedule_is_informational(self):
        schedule_check = self.checks()["schedule"]
        self.assertTrue(schedule_check["ok"])
        self.assertFalse(schedule_check["required"])
        self.assertIn("Turn it on in Settings › Schedule", schedule_check["detail"])

        self.plist.write_text(schedule.render("/bin/cairn", 6, 5), encoding="utf-8")
        self.assertIn("Daily at 6:05", self.checks()["schedule"]["detail"])

        self.plist.write_text("not a plist", encoding="utf-8")
        schedule_check = self.checks()["schedule"]
        self.assertTrue(schedule_check["ok"])
        self.assertIn("Couldn't read the schedule", schedule_check["detail"])

    def test_missing_pdftotext_is_optional(self):
        self.binaries.discard("pdftotext")
        pdftotext = self.checks()["pdftotext"]
        self.assertFalse(pdftotext["ok"])
        self.assertFalse(pdftotext["required"])
        self.assertEqual(pdftotext["fix"], "brew install poppler")
        self.assertEqual(self.cli_doctor()[0], 0)

    def test_cli_prints_marks_and_fixes_and_fails_on_a_required_check(self):
        self.binaries.discard("claude")
        code, out = self.cli_doctor()
        self.assertEqual(code, 1)
        self.assertIn("✓ python: Python", out)
        self.assertIn("✗ claude: Claude Code isn't installed on this Mac", out)
        self.assertIn("    fix: install Claude Code", out)

    def cli_doctor(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cli.cmd_doctor(SimpleNamespace())
        return code, out.getvalue()


class ProviderDoctorTest(unittest.TestCase):
    def setUp(self):
        self.home = llm_home(self, llm_provider="groq")
        for name in HOME_FILES:
            (self.home / name).write_text("", encoding="utf-8")
        self.sent = []
        self.answer = ("OK", None)
        self.enterContext(mock.patch.object(llm, "send", self.fake_send))
        self.enterContext(mock.patch.object(llm, "_next_slot", {}))
        for name in ("_schedule", "_autostart", "_pdftotext"):
            self.enterContext(mock.patch.object(doctor, name, lambda: (True, "", "")))

    def fake_send(self, target, prompt, timeout, retries):
        self.sent.append((target, timeout, retries))
        return self.answer

    def checks(self):
        return {c["name"]: c for c in doctor.checks()}

    def test_a_provider_replaces_the_claude_check_with_key_and_answer(self):
        secrets.set_key("groq", KEY)
        checks = self.checks()
        self.assertEqual(list(checks)[:4], ["python", "llm key", "llm", "home"])
        self.assertEqual(checks["llm key"]["detail"], "Groq key saved")
        self.assertTrue(checks["llm"]["ok"])
        self.assertEqual(checks["llm"]["detail"], "Groq answered")
        target, timeout, retries = self.sent[0]
        self.assertEqual((target.key, timeout, retries), (KEY, 20, 0))

    def test_a_missing_key_fails_both_checks_without_a_call(self):
        checks = self.checks()
        self.assertFalse(checks["llm key"]["ok"])
        self.assertEqual(checks["llm key"]["detail"], "No Groq key yet")
        self.assertEqual(checks["llm key"]["fix"], "open Settings › AI provider and paste a key "
                                                   "from https://console.groq.com/keys")
        self.assertFalse(checks["llm"]["ok"])
        self.assertEqual(self.sent, [])

    def test_a_provider_that_does_not_answer(self):
        secrets.set_key("groq", KEY)
        self.answer = (None, "Groq HTTP 401: Invalid API Key")
        llm_check = self.checks()["llm"]
        self.assertFalse(llm_check["ok"])
        self.assertEqual(llm_check["detail"], "Groq HTTP 401: Invalid API Key")
        self.assertIn("Settings › AI provider", llm_check["fix"])

    def test_ollama_needs_no_key_check(self):
        settings.use(dataclasses.replace(settings.get(), llm_provider="ollama"))
        self.assertEqual(list(self.checks())[:3], ["python", "llm", "home"])

    def test_the_answer_check_is_optional_and_paced(self):
        secrets.set_key("groq", KEY)
        self.answer = (None, "Groq HTTP 429: slow down")
        with mock.patch.object(llm, "_pace") as pace:
            checks = self.checks()
        pace.assert_called_once_with("groq")
        self.assertFalse(checks["llm"]["ok"])
        self.assertFalse(checks["llm"]["required"])
        self.assertTrue(checks["llm key"]["required"])
        self.assertEqual(self.cli_code(), 0)

    def test_a_recent_answer_skips_the_call(self):
        secrets.set_key("groq", KEY)
        answered = datetime.datetime.now(datetime.UTC) - datetime.timedelta(minutes=42)
        llm.check_file().write_text(json.dumps({"groq": answered.isoformat()}), encoding="utf-8")
        check = self.checks()["llm"]
        self.assertEqual((check["ok"], check["detail"]),
                         (True, "Answered 42 min ago"))
        self.assertEqual(self.sent, [])

    def test_an_answer_over_a_day_old_is_asked_again(self):
        secrets.set_key("groq", KEY)
        answered = datetime.datetime.now(datetime.UTC) - datetime.timedelta(hours=25)
        llm.check_file().write_text(json.dumps({"groq": answered.isoformat()}), encoding="utf-8")
        self.assertTrue(self.checks()["llm"]["ok"])
        self.assertEqual(len(self.sent), 1)

    def cli_code(self):
        with contextlib.redirect_stdout(io.StringIO()):
            return cli.cmd_doctor(SimpleNamespace())


if __name__ == "__main__":
    unittest.main()
