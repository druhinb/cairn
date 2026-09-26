"""The update check: GitHub's latest release against this version, cached for a day.

urlopen and the package metadata are replaced, so no test reaches GitHub.
"""
import contextlib
import datetime
import email.message
import io
import json
import unittest
from types import SimpleNamespace
from unittest import mock

from fastapi import FastAPI
from fastapi.testclient import TestClient
from helpers import temp_home

from cairn import __version__, cli, events, update
from cairn.server import system

REPO = "https://github.com/someone/cairn"
RELEASE = {"tag_name": "v9.1.0", "html_url": f"{REPO}/releases/tag/v9.1.0",
           "published_at": "2026-09-01T00:00:00Z"}


def _metadata(*urls):
    message = email.message.Message()
    for url in urls:
        message["Project-URL"] = url
    return message


class VersionTest(unittest.TestCase):
    def test_compares_as_semver(self):
        for latest, current, newer in [("0.3.0", "0.2.0", True), ("v0.2.1", "0.2.0", True),
                                       ("0.10.0", "0.9.9", True), ("1.0.0", "1.0.0", False),
                                       ("0.1.9", "0.2.0", False),
                                       ("1.0.0-rc.1", "1.0.0", False),
                                       ("1.0.0", "1.0.0-rc.1", True),
                                       ("1.0.0rc1", "1.0.0", False),
                                       ("1.0.0", "1.0.0rc1", True),
                                       ("1.0.0b1", "1.0.0a2", True),
                                       ("1.0.0rc1", "1.0.0b3", True),
                                       ("0.3.0", "0.3.0.dev1", True),
                                       ("1.2", "1.2.0", False), ("1.2.0", "1.2", False),
                                       ("1.2.1", "1.2", True),
                                       ("nightly", "0.2.0", False)]:
            with self.subTest(latest=latest, current=current):
                self.assertIs(update.is_newer(latest, current), newer)


class CheckTest(unittest.TestCase):
    def setUp(self):
        self.home = self.enterContext(temp_home())
        self.enterContext(mock.patch.object(
            update.metadata, "metadata",
            return_value=_metadata(f"Homepage, {REPO}", f"Repository, {REPO}")))
        self.urlopen = self.enterContext(mock.patch.object(update.urllib.request, "urlopen"))
        self.urlopen.side_effect = lambda request, timeout: io.BytesIO(
            json.dumps(RELEASE).encode())
        self.warnings = []
        self.addCleanup(events.subscribe(
            lambda event: event.kind == "warn" and self.warnings.append(event.data["text"])))
        self.enterContext(mock.patch.object(update, "_placeholder_warned", False))

    def age_cache(self, **ago):
        cached = json.loads(update.cache_file().read_text(encoding="utf-8"))
        then = datetime.datetime.now(datetime.UTC) - datetime.timedelta(**ago)
        cached["checked_at"] = then.isoformat()
        update.cache_file().write_text(json.dumps(cached), encoding="utf-8")

    def test_reports_a_newer_release(self):
        self.assertEqual(update.check(), {
            "current": __version__, "latest": "9.1.0", "url": RELEASE["html_url"],
            "published_at": RELEASE["published_at"], "newer": True})
        (request,), kwargs = self.urlopen.call_args
        self.assertEqual(request.full_url,
                         "https://api.github.com/repos/someone/cairn/releases/latest")
        self.assertEqual(kwargs, {"timeout": update.TIMEOUT})

    def test_asks_github_once_a_day_unless_forced(self):
        first = update.check()
        self.assertEqual(update.check(), first)
        self.assertEqual(self.urlopen.call_count, 1)
        update.check(force=True)
        self.assertEqual(self.urlopen.call_count, 2)

    def test_a_day_old_answer_is_asked_again(self):
        update.check()
        cached = json.loads(update.cache_file().read_text(encoding="utf-8"))
        day_ago = datetime.datetime.now(datetime.UTC) - datetime.timedelta(hours=25)
        cached["checked_at"] = day_ago.isoformat()
        update.cache_file().write_text(json.dumps(cached), encoding="utf-8")
        update.check()
        self.assertEqual(self.urlopen.call_count, 2)

    def test_a_failed_request_is_none_and_waits_an_hour(self):
        self.urlopen.side_effect = OSError("offline")
        self.assertIsNone(update.check())
        self.assertIsNone(update.check())
        self.assertEqual(self.urlopen.call_count, 1)
        self.assertEqual(self.warnings, ["update check failed: OSError: offline"])
        self.age_cache(minutes=59)
        update.check()
        self.assertEqual(self.urlopen.call_count, 1)
        self.age_cache(minutes=61)
        update.check()
        self.assertEqual(self.urlopen.call_count, 2)
        update.check(force=True)
        self.assertEqual(self.urlopen.call_count, 3)

    def test_a_good_answer_is_kept_for_a_day(self):
        update.check()
        self.age_cache(hours=23)
        update.check()
        self.assertEqual(self.urlopen.call_count, 1)

    def test_a_release_page_off_github_is_dropped(self):
        for url in ("http://github.com/someone/cairn/releases/tag/v9.1.0",
                    "https://evil.example/releases/v9.1.0", "javascript:alert(1)", None):
            with self.subTest(url):
                self.urlopen.side_effect = lambda request, timeout, url=url: io.BytesIO(
                    json.dumps({**RELEASE, "html_url": url}).encode())
                found = update.check(force=True)
                self.assertTrue(found["newer"])
                self.assertIsNone(found["url"])

    def test_an_unreadable_cache_is_asked_again(self):
        update.cache_file().write_text("{not json", encoding="utf-8")
        self.assertTrue(update.check()["newer"])

    def test_never_raises(self):
        self.urlopen.side_effect = RuntimeError("surprise")
        self.assertIsNone(update.check())
        self.assertEqual(self.warnings, ["update check failed: RuntimeError: surprise"])

    def test_the_owner_placeholder_skips_the_check(self):
        placeholder = "https://github.com/<owner>/cairn"
        with mock.patch.object(update.metadata, "metadata",
                               return_value=_metadata(f"Repository, {placeholder}")):
            self.assertIsNone(update.check(force=True))
            self.assertIsNone(update.check())
        self.urlopen.assert_not_called()
        self.assertFalse(update.cache_file().exists())
        self.assertEqual(len(self.warnings), 1)

    def test_api_forces_a_check_only_on_post(self):
        app = FastAPI()
        app.include_router(system.router)
        client = self.enterContext(TestClient(app))
        self.assertTrue(client.get("/api/update").json()["newer"])
        self.assertEqual(client.get("/api/update?force=1").json()["latest"], "9.1.0")
        self.assertEqual(self.urlopen.call_count, 1)
        self.assertTrue(client.post("/api/update").json()["newer"])
        self.assertEqual(self.urlopen.call_count, 2)

    def test_cli_exits_1_when_the_check_fails(self):
        self.urlopen.side_effect = OSError("offline")
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.cmd_update_check(SimpleNamespace(force=False)), 1)

    def test_cli_prints_one_line(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cli.cmd_update_check(SimpleNamespace(force=False))
        self.assertEqual(code, 0)
        self.assertEqual(out.getvalue(), f"Cairn 9.1.0 is out (you have {__version__}): "
                                         f"{RELEASE['html_url']}\n")


if __name__ == "__main__":
    unittest.main()
