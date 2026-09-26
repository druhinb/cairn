"""Commands without a module of their own: `icons` and `llm`, and how main() turns
an error into an exit status."""
import contextlib
import io
import os
import stat
import sys
import time
import unittest
from unittest import mock

from helpers import temp_home
from ai.test_llm import KEY, llm_home

from cairn import cli, store
from cairn.ai import llm
from cairn.core import paths, secrets, settings
from cairn.jobs import logos, pipeline
from cairn.system import schedule


def _posting(posting_id, company):
    return {"id": posting_id, "company_name": company, "title": "Software Engineer",
            "url": f"https://jobs.lever.co/x/{posting_id}", "active": True,
            "is_visible": True, "date_posted": time.time()}


class IconsCommandTest(unittest.TestCase):
    def setUp(self):
        self.enterContext(temp_home())
        store.upsert_postings([_posting("a", "Acme"), _posting("b", "Globex")], "feed")
        self.fetch_all = self.enterContext(
            mock.patch.object(logos, "fetch_all", return_value=logos.Job(2, False)))
        self.info = self.enterContext(mock.patch.object(cli.ui, "info"))
        self.error = self.enterContext(mock.patch.object(cli.ui, "error"))

    def _icons(self, *argv):
        return cli.cmd_icons(cli._parser().parse_args(["icons", *argv]))

    def test_fetches_everything_by_default_and_up_to_a_limit_when_given(self):
        self.assertEqual(self._icons(), 0)
        self.fetch_all.assert_called_once_with(limit=None)
        self.assertEqual(self._icons("--limit", "300"), 0)
        self.fetch_all.assert_called_with(limit=300)
        self.info.assert_called_with(
            "fetched 2 icons. 0 of 2 companies with active postings now have one.")

    def test_reset_failures_retries_what_failed_before(self):
        store.save_company("acme", None, None, "no guess")
        store.save_company("globex", "globex.com", "clearbit")
        store.save_logo("globex.com", None, "HTTP 404")
        self.assertEqual(self._icons(), 0)
        self.assertEqual(store.companies_without_domain(10), [])
        self.assertEqual(self._icons("--reset-failures"), 0)
        self.assertEqual([c[0] for c in store.companies_without_domain(10)], ["acme"])
        self.assertEqual(store.logos(), {})
        self.info.assert_any_call("cleared 1 failed website lookup and 1 failed icon download.")

    def test_one_icon_is_one_icon(self):
        self.fetch_all.return_value = logos.Job(1, False)
        self.assertEqual(self._icons(), 0)
        self.info.assert_called_with(
            "fetched 1 icon. 0 of 2 companies with active postings now have one.")

    def test_a_run_holding_the_lock_does_not_stop_it(self):
        with pipeline.run_lock():
            self.assertEqual(self._icons(), 0)
        self.fetch_all.assert_called_once_with(limit=None)

    def test_refused_while_another_icon_fetch_runs(self):
        with logos.icon_job():
            self.assertEqual(self._icons("--reset-failures"), 3)
        self.fetch_all.assert_not_called()
        self.error.assert_called_once_with("a company icon fetch is already running")

    def test_the_fetch_holds_the_icon_marker_and_leaves_the_run_lock_free(self):
        held = []

        def fetch_all(limit):
            held.append((logos.icons_running(), pipeline.lock_holder()))
            return logos.Job(0, False)
        self.fetch_all.side_effect = fetch_all
        self.assertEqual(self._icons(), 0)
        self.assertEqual(held, [(True, None)])
        self.assertFalse(logos.icons_running())

    def test_exits_1_when_the_network_is_down(self):
        self.fetch_all.return_value = logos.Job(0, True)
        self.assertEqual(self._icons(), 1)

    def test_refused_when_company_icons_are_off(self):
        with temp_home(company_icons=False):
            self.assertEqual(self._icons(), 1)
        self.fetch_all.assert_not_called()
        self.error.assert_called_once_with(
            "company icons are off. Set company_icons = true in config.toml")


def run_main(*argv, stdin=""):
    """(exit status, everything printed) for `cairn *argv`."""
    out = io.StringIO()
    with mock.patch.object(sys, "argv", ["cairn", *argv]), \
            mock.patch.object(sys, "stdin", io.StringIO(stdin)), \
            mock.patch.object(cli.ui, "attach"), \
            contextlib.redirect_stdout(out), contextlib.redirect_stderr(out), \
            unittest.TestCase().assertRaises(SystemExit) as exited:
        cli.main()
    return exited.exception.code, out.getvalue()


class LlmCommandTest(unittest.TestCase):
    def setUp(self):
        self.home = llm_home(self)

    def test_shows_the_active_provider(self):
        code, out = run_main("llm")
        self.assertEqual(code, 0)
        self.assertIn("provider  claude-code (Claude Code)", out)
        self.assertIn("key  none", out)

    def test_set_stores_the_key_apart_from_the_settings(self):
        code, out = run_main("llm", "set", "groq", "--model", "llama-x", "--key-stdin",
                             stdin=f"{KEY}\n")
        self.assertEqual(code, 0, out)
        self.assertIn("key  present", out)
        self.assertNotIn(KEY, out)
        self.assertEqual(secrets.get_key("groq"), KEY)
        self.assertEqual((settings.get().llm_provider, settings.get().llm_model),
                         ("groq", "llama-x"))
        self.assertNotIn(KEY, paths.config_file().read_text(encoding="utf-8"))

    def test_a_bad_key_on_stdin_exits_2_with_one_line(self):
        code, out = run_main("llm", "set", "groq", "--key-stdin", stdin="two words\n")
        self.assertEqual((code, out),
                         (2, "ERROR the key should be printable ASCII with no spaces\n"))
        self.assertFalse(paths.config_file().exists())
        self.assertFalse(secrets.secrets_file().exists())

    def test_set_keeps_the_rules_of_the_api(self):
        for argv, message in [
                (("gpt",), "unknown provider 'gpt'"),
                (("custom",), "--base-url: the custom provider needs one"),
                (("anthropic", "--base-url", "https://evil.example"),
                 "--base-url: only ollama and custom take one"),
                (("ollama", "--base-url", "ftp://x"), "--base-url: should be an http(s) URL")]:
            with self.subTest(argv):
                code, out = run_main("llm", "set", *argv, stdin="")
                self.assertEqual(code, 2)
                self.assertIn(message, out)
        self.assertFalse(paths.config_file().exists())
        code, out = run_main("llm", "set", "custom", "--base-url", "https://llm.test/v1",
                             "--model", "m", stdin="")
        self.assertEqual(code, 0, out)
        self.assertIn("base url  https://llm.test/v1", out)

    def test_a_failed_test_call_exits_1_without_the_key(self):
        secrets.set_key("groq", KEY)
        settings.use(settings.from_dict({"llm_provider": "groq"}))
        with mock.patch.object(llm, "send", return_value=(None, f"Groq HTTP 401: bad {KEY}")):
            code, out = run_main("llm", "test")
        self.assertEqual(code, 1)
        self.assertIn("Groq HTTP 401: bad [key]", out)
        self.assertNotIn(KEY, out)
        with mock.patch.object(llm, "send", return_value=("OK", None)):
            self.assertEqual(run_main("llm", "test")[0], 0)


class MainTest(unittest.TestCase):
    def setUp(self):
        self.enterContext(temp_home())

    def test_secrets_and_schedule_errors_exit_2_with_one_line(self):
        secrets.secrets_file().write_text("[keys\n", encoding="utf-8")
        code, out = run_main("llm")
        self.assertEqual(code, 2)
        self.assertTrue(out.splitlines()[-1].startswith(
            f"ERROR {secrets.secrets_file()} is not valid TOML"), out)
        self.assertNotIn("Traceback", out)
        with mock.patch.object(cli, "cmd_status",
                               side_effect=schedule.ScheduleError("launchctl list failed")):
            self.assertEqual(run_main("status"), (2, "ERROR launchctl list failed\n"))

    def test_the_help_lists_every_command(self):
        code, out = run_main("--help")
        self.assertEqual(code, 0)
        self.assertIn("settings,llm}", out)


@unittest.skipIf(sys.platform == "win32", "Windows files have no Unix permission bits")
class LockDownTest(unittest.TestCase):
    def setUp(self):
        self.root = self.enterContext(temp_home())
        previous = os.umask(0o022)
        self.addCleanup(os.umask, previous)

    def mode(self, path):
        return stat.S_IMODE(path.lstat().st_mode)

    def test_an_open_home_becomes_owner_only_and_so_do_new_files(self):
        home = self.root / "home"
        home.mkdir(0o755)
        (home / "profile.md").write_text("resume", encoding="utf-8")
        (home / "run.lock").write_text("1", encoding="utf-8")
        (home / "run.lock").chmod(0o640)
        (home / "logos").mkdir(0o755)
        outside = self.root / "outside.txt"
        outside.write_text("x", encoding="utf-8")
        (home / "link").symlink_to(outside)
        with mock.patch.dict(os.environ, CAIRN_HOME=str(home)):
            paths.lock_down()
        self.assertEqual({name: self.mode(home / name) for name in
                          ("", "profile.md", "run.lock", "logos")},
                         {"": 0o700, "profile.md": 0o600, "run.lock": 0o600, "logos": 0o700})
        self.assertEqual(self.mode(outside), 0o644)
        (home / "new.txt").write_text("x", encoding="utf-8")
        self.assertEqual(self.mode(home / "new.txt"), 0o600)

    def test_every_command_locks_the_home_down(self):
        self.root.chmod(0o755)
        paths.profile_md().write_text("resume", encoding="utf-8")
        paths.profile_md().chmod(0o644)
        run_main("status")
        self.assertEqual((self.mode(self.root), self.mode(paths.profile_md())), (0o700, 0o600))


class WindowsOutputTest(unittest.TestCase):
    def test_without_a_console_output_goes_to_files_in_the_home(self):
        home = self.enterContext(temp_home())
        with mock.patch.object(sys, "stdout", None), mock.patch.object(sys, "stderr", None):
            cli._windows_output()
            opened = sys.stdout, sys.stderr
            print("✓ ranked Zürich AG")
            print("boom", file=sys.stderr)
        for stream in opened:
            stream.close()
        self.assertEqual((home / "background.out").read_text(encoding="utf-8"),
                         "✓ ranked Zürich AG\n")
        self.assertEqual((home / "background.err").read_text(encoding="utf-8"), "boom\n")

    def test_a_redirected_stream_writes_utf8(self):
        raw = io.BytesIO()
        stream = io.TextIOWrapper(raw, encoding="cp1252", newline="\n")
        with mock.patch.object(sys, "stdout", stream), mock.patch.object(sys, "stderr", stream):
            cli._windows_output()
            print("✓")
        stream.flush()
        self.assertEqual(raw.getvalue(), "✓\n".encode())


if __name__ == "__main__":
    unittest.main()
