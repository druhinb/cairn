"""Notifications are best-effort and must never take a run down with them.

By the time these fire the ranked postings are already stored, so a failed push is
an inconvenience. An exception escaping here would turn that into a failed run — and,
worse, one that failed *after* the commit point.
"""
import dataclasses
import datetime
import unittest
import urllib.error
from unittest import mock

from helpers import temp_home

from cairn import store
from cairn.core import settings
from cairn.jobs import notify


def _settings(**overrides):
    settings.use(dataclasses.replace(settings.get(), **overrides))


class DisabledTest(unittest.TestCase):
    def setUp(self):
        self.enterContext(temp_home())

    def test_no_channels_configured_sends_nothing(self):
        self.assertEqual(notify.send("t", "m"), [])

    def test_run_finished_is_a_noop_when_disabled(self):
        self.assertEqual(notify.run_finished([{"company_name": "Acme"}]), [])

    def test_a_blank_topic_is_not_treated_as_a_topic(self):
        _settings(notify_ntfy_topic="   ")
        self.assertEqual(notify.send("t", "m"), [])


class FailureTest(unittest.TestCase):
    def setUp(self):
        self.enterContext(temp_home(notify_ntfy_topic="a-topic"))
        self._ntfy = notify._ntfy

    def tearDown(self):
        notify._ntfy = self._ntfy

    def test_a_network_failure_is_swallowed(self):
        def boom(*_a, **_kw):
            raise urllib.error.URLError("no route to host")
        notify._ntfy = boom
        self.assertEqual(notify.send("t", "m"), [])   # must not raise

    def test_a_timeout_is_swallowed(self):
        def boom(*_a, **_kw):
            raise TimeoutError("slow")
        notify._ntfy = boom
        self.assertEqual(notify.send("t", "m"), [])

    def test_a_header_value_urllib_refuses_to_send_is_swallowed(self):
        """A CR/LF or non-latin-1 posting URL used to raise past this point and take
        the run down with it, after the run's own commit."""
        def boom(*_a, **_kw):
            raise ValueError("Invalid return character or leading space in header")
        notify._ntfy = boom
        self.assertEqual(notify.send("t", "m"), [])


class UnsafeUrlTest(unittest.TestCase):
    """A url the Actions header cannot carry drops the button rather than raising
    or letting the url break out of its field."""

    def test_a_delimiter_in_the_url_drops_the_action(self):
        for url in ("https://x.test/j;evil", "https://x.test/j,evil"):
            with self.subTest(url):
                self.assertIsNone(notify._action({"company_name": "Acme", "url": url}))

    def test_a_cr_or_lf_in_the_url_drops_the_action(self):
        self.assertIsNone(notify._action(
            {"company_name": "Acme", "url": "https://x.test/j\r\nX-Injected: 1"}))

    def test_a_non_latin1_url_drops_the_action(self):
        self.assertIsNone(notify._action({"company_name": "Acme", "url": "https://x.test/日本"}))

    def test_a_safe_url_still_gets_a_button(self):
        action = notify._action({"company_name": "Acme", "url": "https://x.test/j"})
        self.assertEqual(action, "view, Open Acme, https://x.test/j")


class MessageTest(unittest.TestCase):
    def setUp(self):
        self.enterContext(temp_home(notify_ntfy_topic="a-topic"))
        self._send, self.sent = notify.send, []

        def capture(title, message, **kwargs):
            self.sent.append(message)
            self.titles.append(title)
            self.kwargs.append(kwargs)
            return ["ntfy"]
        self.titles, self.kwargs = [], []
        notify.send = capture

    def tearDown(self):
        notify.send = self._send

    def test_below_floor_postings_are_never_the_lead(self):
        results = [{"company_name": "Deloitte", "title": "Analyst", "fit": 80,
                    "tier": 10, "below_floor": True},
                   {"company_name": "Citadel", "title": "SWE", "fit": 90,
                    "tier": 95, "below_floor": False}]
        notify.run_finished(results)
        self.assertIn("Citadel", self.sent[0])
        self.assertNotIn("Deloitte", self.sent[0])

    def test_an_empty_run_still_says_something(self):
        notify.run_finished([])
        self.assertIn("No new postings", self.titles[0])

    def test_an_exceptional_match_raises_priority(self):
        results = [{"company_name": "Jane Street", "title": "SWE", "fit": 95,
                    "tier": 98, "below_floor": False, "url": "https://x.test/j"}]
        notify.run_finished(results)
        self.assertEqual(self.kwargs[0]["priority"], "high")

    def test_an_ordinary_day_stays_at_default_priority(self):
        results = [{"company_name": "Acme", "title": "SWE", "fit": 65,
                    "tier": 60, "below_floor": False, "url": "https://x.test/j"}]
        notify.run_finished(results)
        self.assertEqual(self.kwargs[0]["priority"], "default")

    def test_nothing_above_the_bar_is_quiet(self):
        results = [{"company_name": "Deloitte", "title": "Analyst", "fit": 20,
                    "tier": 10, "below_floor": True}]
        notify.run_finished(results)
        self.assertEqual(self.kwargs[0]["priority"], "low")

    def test_the_body_counts_what_the_top_lines_leave_out(self):
        results = [{"company_name": f"Co{i}", "title": "SWE", "fit": 90, "tier": 80,
                    "below_floor": False} for i in range(5)]
        results.append({"company_name": "Low", "title": "SWE", "fit": 10, "tier": 80,
                        "below_floor": False})
        notify.run_finished(results)
        self.assertEqual(self.titles[0], "5 strong matches today")
        self.assertTrue(self.sent[0].endswith("+2 more strong · 1 other ranked"))

    def test_action_buttons_point_at_the_postings(self):
        results = [{"company_name": "Citadel", "title": "SWE", "fit": 90, "tier": 95,
                    "below_floor": False, "url": "https://citadel.test/job"}]
        notify.run_finished(results)
        actions = self.kwargs[0]["actions"]
        self.assertTrue(any("https://citadel.test/job" in a for a in actions))

    def test_a_comma_in_a_company_name_cannot_break_the_actions_header(self):
        """Commas and semicolons delimit ntfy's Actions header."""
        results = [{"company_name": "Acme, Inc; Ltd", "title": "SWE", "fit": 90,
                    "tier": 95, "below_floor": False, "url": "https://x.test/j"}]
        notify.run_finished(results)
        label = self.kwargs[0]["actions"][0].split(",")[1]
        self.assertNotIn(";", label)


class FollowUpLineTest(unittest.TestCase):
    STRONG = [{"company_name": "Citadel", "title": "SWE", "fit": 90, "tier": 95,
               "below_floor": False}]
    setUp, tearDown = MessageTest.setUp, MessageTest.tearDown

    def _stale(self, *companies):
        month_ago = (datetime.datetime.now() - datetime.timedelta(days=30)).isoformat(
            timespec="seconds")
        for n, company in enumerate(companies):
            store.upsert_postings([{"id": f"p{n}", "company_name": company, "title": "SWE",
                                    "url": f"https://x.test/{n}", "active": True,
                                    "is_visible": True}], "feed")
            with mock.patch.object(store.db, "now", return_value=month_ago):
                store.set_status(f"p{n}", "applied")

    def test_the_push_names_the_applications_waiting_on_a_follow_up(self):
        self._stale("Acme", "Globex", "Initech")
        notify.run_finished(self.STRONG)
        self.assertTrue(self.sent[0].endswith(
            "\n\n3 applications need a follow-up: Acme, Globex"), self.sent[0])

    def test_rows_passed_in_are_used_as_given(self):
        notify.run_finished([], follow_ups=[{"company": "Hooli"}])
        self.assertEqual(self.sent[0], "The run ranked nothing new.\n\n"
                                       "1 application needs a follow-up: Hooli")

    def test_no_line_when_nothing_waits(self):
        notify.run_finished(self.STRONG)
        self.assertNotIn("follow-up", self.sent[0])

    def test_stored_times_that_are_not_iso_leave_the_push_intact(self):
        self._stale("Acme", "Globex")
        with store.connect() as conn:
            conn.execute("UPDATE applications SET applied_at = '07/27/2026', "
                         "updated_at = '07/27/2026' WHERE posting_id = 'p0'")
            conn.execute("UPDATE application_events SET at = '07/27/2026' "
                         "WHERE posting_id = 'p0'")
            conn.execute("UPDATE application_events SET at = '2026-07-27T09:00:00Z' "
                         "WHERE posting_id = 'p1'")
        notify.run_finished(self.STRONG)
        self.assertTrue(self.sent[0].endswith("\n\n1 application needs a follow-up: Globex"),
                        self.sent[0])

    def test_the_line_can_be_turned_off(self):
        self._stale("Acme")
        cfg = dataclasses.replace(settings.get(), notify_follow_ups=False)
        with mock.patch.object(settings, "get", return_value=cfg):
            notify.run_finished(self.STRONG)
        self.assertNotIn("follow-up", self.sent[0])


class SuiteIsolationTest(unittest.TestCase):
    """The temp_home fixture must leave every notification channel off.

    test_state drives cmd_run end to end, and against the real config.toml that
    sent a live push to a real phone on every `unittest discover` — small fixtures
    produced a stream of "1 new, none above your bar" alerts.
    """

    def test_the_test_fixture_disables_notifications(self):
        self.enterContext(temp_home())
        self.assertEqual(settings.get().notify_ntfy_topic, "")
        self.assertFalse(settings.get().notify_macos)


if __name__ == "__main__":
    unittest.main()
