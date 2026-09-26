"""launchd sends stdout to a file, so off a TTY the UI must emit no terminal control
bytes at all.

This is easy to regress: rich's `no_color` strips colour but keeps attributes, so a
bold style still writes "\\x1b[1m". The guard is `force_terminal=IS_TTY`, and these
tests run the UI in a subprocess with stdout redirected to prove it holds — including
with FORCE_COLOR set, which is exactly what defeats naive terminal detection.
"""
import os
import subprocess
import sys
import tempfile
import unittest

import helpers  # noqa: F401 - installs the suite settings guard

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SRC = os.path.join(REPO, "src")

# Every renderer, including the styled paths.
EXERCISE_UI = """
from cairn.core import events, ui
ui.attach()
events.emit("info", text="plain line")
events.emit("warn", text="a warning")
ui.error("an error")
events.emit("phase_start", name="a phase")
ui.summary([("label", "value")], title="a title")
ui.postings([{"category": "Software", "company_name": "Acme",
              "title": "Engineer [New Grad]", "id": "abcdef123456",
              "fit": 82, "repeat": True}], extra=3)
ui.recent_applications([("abcdef123456", {"applied_at": "2026-07-21",
                                          "company": "Acme", "title": "SWE"})])
events.emit("phase_end", name="a phase", seconds=3.0)
"""


def _env(env_extra, home):
    """A subprocess environment with its own empty CAIRN_HOME."""
    env = {**os.environ, "PYTHONPATH": SRC, "CAIRN_HOME": home}
    env.pop("FORCE_COLOR", None)
    env.update(env_extra or {})
    return env


def _run(env_extra=None):
    with tempfile.TemporaryDirectory() as home:
        # capture_output gives a pipe, not a tty — the same shape as the log redirect.
        return subprocess.run([sys.executable, "-c", EXERCISE_UI], cwd=REPO,
                              env=_env(env_extra, home), capture_output=True, timeout=60)


def _cli(args, env_extra=None):
    with tempfile.TemporaryDirectory() as home:
        return subprocess.run([sys.executable, "-m", "cairn.cli", *args], cwd=REPO,
                              env=_env(env_extra, home), capture_output=True, timeout=60)


class ArgparseOutputTest(unittest.TestCase):
    """Python 3.14's argparse colourises its own usage/error output, which ui.py
    cannot intercept. Left on, a bad argument under launchd writes escapes into
    launchd.err."""

    def test_usage_error_has_no_escapes(self):
        proc = _cli(["not-a-subcommand"], {"FORCE_COLOR": "1"})
        self.assertNotEqual(proc.returncode, 0)
        self.assertNotIn(b"\x1b", proc.stdout + proc.stderr)

    def test_help_has_no_escapes(self):
        for args in (["--help"], ["run", "--help"]):
            proc = _cli(args, {"FORCE_COLOR": "1"})
            self.assertEqual(proc.returncode, 0)
            self.assertNotIn(b"\x1b", proc.stdout + proc.stderr, f"for {args}")

    def test_bad_flag_value_has_no_escapes(self):
        proc = _cli(["fetch", "--limit", "-1"], {"FORCE_COLOR": "1"})
        self.assertEqual(proc.returncode, 2)
        self.assertNotIn(b"\x1b", proc.stdout + proc.stderr)


class OffTtyOutputTest(unittest.TestCase):
    def test_no_escape_bytes_when_stdout_is_not_a_tty(self):
        out = _run().stdout
        self.assertNotIn(b"\x1b", out)
        self.assertNotIn(b"\r", out.replace(b"\r\n", b"\n"))

    def test_force_color_does_not_reintroduce_escapes(self):
        """FORCE_COLOR makes rich's own is_terminal true; isatty must still win."""
        out = _run({"FORCE_COLOR": "1"}).stdout
        self.assertNotIn(b"\x1b", out)
        self.assertNotIn(b"\r", out.replace(b"\r\n", b"\n"))

    def test_markup_in_a_title_is_not_swallowed(self):
        out = _run().stdout
        self.assertIn(b"Engineer [New Grad]", out)

    def test_plain_output_keeps_the_full_id_for_the_log(self):
        """The TTY table shows a short prefix; the log must stay copy-pasteable."""
        out = _run().stdout
        self.assertIn(b"abcdef123456", out)

    def test_every_renderer_actually_produced_output(self):
        proc = _run()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        for expected in (b"plain line", b"WARN", b"ERROR", b"a phase",
                         b"a title", b"Acme", b"and 3 more", b"[a phase] 3.0s"):
            self.assertIn(expected, proc.stdout)


if __name__ == "__main__":
    unittest.main()
