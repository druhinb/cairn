"""The insights module and its routes: skills gaps, apply-link checks, the relevance
explainer, suggested companies, application stats, feedback, and facets."""
import datetime
import queue
import threading
import time
import unittest
from unittest import mock

from fastapi import FastAPI
from fastapi.testclient import TestClient
from helpers import temp_home
from test_net import drip, serve_web

from cairn import store
from cairn.core import events, settings
from cairn.jobs import fetch, insights
from cairn.server import insights as insights_routes

NOW = time.time()
DAY = 86400


def _row(posting_id, title="Software Engineer", company="Acme", **fields):
    row = {"id": posting_id, "company_name": company, "title": title,
           "url": f"https://jobs.test/{posting_id}", "locations": ["Seattle, WA"],
           "category": "Software", "active": True, "is_visible": True,
           "date_posted": NOW, "date_updated": NOW}
    row.update(fields)
    return row


def _summary(met, missing):
    return f"TECH: Go\nDOMAIN: infra\nMUST: BS\nSIGNALS: scale\nMET: {met}\nMISSING: {missing}"


GREENHOUSE = "https://job-boards.greenhouse.io"
WORKDAY = "https://acme.wd5.myworkdayjobs.com"
CAREERS = "https://careers.acme.com"


def _ok():
    return 200, {}, b""


def _head_refused(handler):
    handler.send_response(405 if handler.command == "HEAD" else 200)
    handler.send_header("Content-Length", "0")
    handler.end_headers()


def _hang_up(handler):
    pass


# each apply link and the status its answer gives
LINKS = {
    f"{CAREERS}/jobs/open": "open",
    f"{CAREERS}/jobs/moved": "open",
    f"{CAREERS}/jobs/to-site-root": "open",
    f"{CAREERS}/jobs/head-refused": "open",
    f"{CAREERS}/gone-404": "closed",
    f"{CAREERS}/gone-410": "closed",
    f"{GREENHOUSE}/acme/jobs/1": "closed",
    f"{GREENHOUSE}/acme/jobs/2": "closed",
    f"{WORKDAY}/en-US/External/job/Seattle-WA/SWE_R12345": "closed",
    f"{WORKDAY}/External/job/Austin-TX/SWE_R12346": "closed",
    "https://jobs.lever.co/acme/0b1c": "closed",
    f"{WORKDAY}/External/job/Remote/SWE_R12347": "unknown",
    f"{CAREERS}/jobs/sso": "unknown",
    f"{CAREERS}/jobs/next": "unknown",
    f"{CAREERS}/jobs/busy": "unknown",
    f"{CAREERS}/jobs/blocked": "unknown",
    f"{CAREERS}/jobs/loop": "unknown",
    "http://127.0.0.1:631/jobs/1": "unknown",
    f"{GREENHOUSE}/acme": "open",
    f"{CAREERS}/jobs/hang-up": None,
}

REPLIES = {
    f"{CAREERS}/jobs/open": _ok(),
    f"{CAREERS}/jobs/moved": (302, {"Location": "/jobs/8"}, b""),
    f"{CAREERS}/jobs/8": _ok(),
    f"{CAREERS}/jobs/to-site-root": (301, {"Location": "/"}, b""),
    f"{CAREERS}/": _ok(),
    f"{CAREERS}/jobs/head-refused": (0, {}, _head_refused),
    f"{CAREERS}/gone-404": (404, {}, b""),
    f"{CAREERS}/gone-410": (410, {}, b""),
    f"{GREENHOUSE}/acme/jobs/1": (302, {"Location": "/acme?error=true"}, b""),
    f"{GREENHOUSE}/acme?error=true": _ok(),
    f"{GREENHOUSE}/acme/jobs/2": (301, {"Location": "/"}, b""),
    f"{GREENHOUSE}/": _ok(),
    f"{GREENHOUSE}/acme": _ok(),
    f"{WORKDAY}/en-US/External/job/Seattle-WA/SWE_R12345": (
        302, {"Location": "/en-US/External"}, b""),
    f"{WORKDAY}/en-US/External": _ok(),
    f"{WORKDAY}/External/job/Austin-TX/SWE_R12346": (302, {"Location": "/External"}, b""),
    f"{WORKDAY}/External": _ok(),
    "https://jobs.lever.co/acme/0b1c": (302, {"Location": "/acme"}, b""),
    "https://jobs.lever.co/acme": _ok(),
    f"{WORKDAY}/External/job/Remote/SWE_R12347": (
        302, {"Location": "/External/login?redirect=%2FExternal"}, b""),
    f"{WORKDAY}/External/login?redirect=%2FExternal": _ok(),
    f"{CAREERS}/jobs/sso": (302, {"Location": "https://id.acme.com/sso/start"}, b""),
    "https://id.acme.com/sso/start": _ok(),
    f"{CAREERS}/jobs/next": (302, {"Location": "/account?next=/jobs/next"}, b""),
    f"{CAREERS}/account?next=/jobs/next": _ok(),
    f"{CAREERS}/jobs/busy": (503, {}, b""),
    f"{CAREERS}/jobs/blocked": (403, {}, b""),
    f"{CAREERS}/jobs/loop": (302, {"Location": "/jobs/loop"}, b""),
    f"{CAREERS}/jobs/hang-up": (0, {}, _hang_up),
}


class SkillsGapTest(unittest.TestCase):
    def setUp(self):
        self.enterContext(temp_home())

    def test_phrases_are_counted_once_per_group_with_aliases_merged(self):
        store.upsert_postings([_row("a", company="Alpha"), _row("b", company="Beta"),
                               _row("c", company="Gamma"), _row("old", company="Delta")],
                              "feed")
        store.upsert_postings([_row("a2", company="Alpha", url="https://other.test/a")],
                              "feed-two")
        store.save_scores([{"id": i, "fit": fit, "tier": 70}
                           for i, fit in (("a", 90), ("a2", 90), ("b", 80), ("c", 70),
                                          ("old", 60))])
        store.set_keywords("a", _summary("Go, Python", "K8s, Rust"))
        store.set_keywords("a2", _summary("Go", "kubernetes"))
        store.set_keywords("b", _summary("golang", "Kubernetes, rust."))
        store.set_keywords("c", _summary("none", "Postgres"))
        store.set_keywords("old", "TECH: Go\nDOMAIN: x\nMUST: y\nSIGNALS: z")
        self.assertEqual(insights.skills_gap(), [
            {"skill": "kubernetes", "missing_in": 2, "met_in": 0},
            {"skill": "rust", "missing_in": 2, "met_in": 0},
            {"skill": "postgresql", "missing_in": 1, "met_in": 0},
            {"skill": "go", "missing_in": 0, "met_in": 2},
            {"skill": "python", "missing_in": 0, "met_in": 1}])
        self.assertEqual([s["skill"] for s in insights.skills_gap(limit=1)],
                         ["kubernetes", "rust", "go", "python"])


class LinkStatusTest(unittest.TestCase):
    def setUp(self):
        self.web, _ = serve_web(self)
        self.web.replies = REPLIES

    def test_each_answer_is_classified(self):
        for url, expected in LINKS.items():
            with self.subTest(url=url):
                self.assertEqual(insights.link_status(url), expected)

    def test_a_head_refused_is_asked_again_with_get(self):
        insights.link_status(f"{CAREERS}/jobs/head-refused")
        self.assertEqual(self.web.methods, ["HEAD", "GET"])

    def test_no_request_outlives_the_deadline(self):
        self.web.replies = {f"{CAREERS}/slow": drip(b"HTTP/1.1 200 OK\r\nX-Slow: ")}
        start = time.monotonic()
        self.assertIsNone(insights.link_status(f"{CAREERS}/slow", time.monotonic() + 0.3))
        self.assertLess(time.monotonic() - start, 0.6)


def _link_statuses():
    return dict(store.connect().execute("SELECT id, link_status FROM postings").fetchall())


class CheckLinksTest(unittest.TestCase):
    def setUp(self):
        self.enterContext(temp_home())
        self.web, _ = serve_web(self)
        self.web.replies = REPLIES

    def _posting(self, posting_id, url, **fields):
        store.upsert_postings([_row(posting_id, company=posting_id, url=url, **fields)],
                              "feed")
        store.save_scores([{"id": posting_id, "fit": 80, "tier": 70}])

    def test_closed_links_are_recorded_and_unknown_ones_keep_their_status(self):
        self._posting("open", f"{CAREERS}/jobs/open")
        self._posting("gone", f"{CAREERS}/gone-404")
        self._posting("busy", f"{CAREERS}/jobs/busy")
        with store.connect() as conn:
            conn.execute("UPDATE postings SET link_status = 'open' WHERE id = 'busy'")
        self.assertEqual(insights.check_links(),
                         {"checked": 3, "closed": 1, "closed_ids": ["gone"]})
        self.assertEqual(_link_statuses(), {"open": "open", "gone": "closed", "busy": "open"})
        self.assertEqual({job["id"]: job["closed"] for job in store.search()[0]},
                         {"open": False, "gone": True, "busy": False})
        self.assertEqual(insights.check_links()["checked"], 0)

    def test_only_active_relevant_ranked_postings_are_checked(self):
        self._posting("ranked", f"{CAREERS}/gone-404")
        self._posting("senior", f"{CAREERS}/gone-410", title="Senior Software Engineer")
        self._posting("inactive", f"{CAREERS}/gone-404/inactive", active=False)
        store.upsert_postings([_row("unranked", url=f"{CAREERS}/gone-404/unranked")], "feed")
        self.assertEqual(insights.check_links()["checked"], 1)
        self.assertEqual(_link_statuses(), {"ranked": "closed", "senior": None,
                                            "inactive": None, "unranked": None})

    def test_ids_pick_the_postings_checked_ranked_or_not(self):
        store.upsert_postings([_row("new", url=f"{CAREERS}/gone-410"),
                               _row("other", company="Other", url=f"{CAREERS}/jobs/open")],
                              "feed")
        self.assertEqual(insights.check_links(ids=["new"]),
                         {"checked": 1, "closed": 1, "closed_ids": ["new"]})
        self.assertEqual(_link_statuses(), {"new": "closed", "other": None})

    def test_a_link_that_got_no_answer_is_tried_next_time(self):
        self._posting("offline", f"{CAREERS}/jobs/hang-up")
        self.assertEqual(insights.check_links()["checked"], 0)
        self.assertIsNone(store.get_posting("offline")["link_checked_at"])

    def test_a_link_checked_more_than_three_days_ago_is_checked_again(self):
        self._posting("gone", f"{CAREERS}/gone-410")
        old = (datetime.datetime.now() - datetime.timedelta(days=4)).isoformat()
        with store.connect() as conn:
            conn.execute("UPDATE postings SET link_status = 'open', link_checked_at = ?",
                         (old,))
        self.assertEqual(insights.check_links()["closed"], 1)

    def test_no_check_outlives_its_budget(self):
        self.web.replies = {f"{CAREERS}/slow": drip(b"HTTP/1.1 200 OK\r\nX-Slow: ")}
        self._posting("slow", f"{CAREERS}/slow")
        start = time.monotonic()
        self.assertEqual(insights.check_links(budget_seconds=0.3)["checked"], 0)
        self.assertLess(time.monotonic() - start, 0.6)
        deadline = time.monotonic() + 1
        while any(t.name.startswith("ThreadPoolExecutor") for t in threading.enumerate()):
            self.assertLess(time.monotonic(), deadline, "a link check thread outlived its budget")
            time.sleep(0.05)

    def test_closed_postings_sort_last_and_are_never_new(self):
        self._posting("closed", f"{CAREERS}/gone-404")
        self._posting("open", f"{CAREERS}/jobs/open")
        store.save_scores([{"id": "open", "fit": 10, "tier": 10}])
        insights.check_links()
        self.assertEqual([job["id"] for job in store.search()[0]], ["open", "closed"])
        store.upsert_postings([_row("unseen-closed", company="Third",
                                    url=f"{CAREERS}/gone-404/unseen")], "feed")
        with store.connect() as conn:
            conn.execute("UPDATE postings SET link_status = 'closed' "
                         "WHERE id = 'unseen-closed'")
        self.assertNotIn("unseen-closed",
                         [j["id"] for j in store.new_postings(settings.get(), 21)])


class LinkLockTest(unittest.TestCase):
    def setUp(self):
        self.enterContext(temp_home())
        self.heard = []
        self.addCleanup(events.subscribe(self.heard.append))

    def test_a_run_skips_its_check_while_another_holds_the_lock(self):
        with (insights.LINK_LOCK,
              mock.patch.object(insights, "check_links") as check_links):
            self.assertIsNone(fetch.check_links())
        check_links.assert_not_called()
        self.assertEqual([e.data["text"] for e in self.heard],
                         ["[links] skipped: another link check is running"])

    def test_the_lock_is_free_after_a_check_that_failed(self):
        with mock.patch.object(insights, "check_links", side_effect=RuntimeError("boom")):
            self.assertIsNone(fetch.check_links())
        self.assertFalse(insights.LINK_LOCK.locked())


class WhyNothingTest(unittest.TestCase):
    def test_each_rule_counts_what_the_earlier_ones_kept(self):
        with temp_home(degrees_held=["Bachelor's"], location_allow=["WA"]):
            store.upsert_postings([
                _row("kept"),
                _row("senior", "Senior Software Engineer"),
                _row("field", "Mechanical Engineer"),
                _row("senior-field", "Senior Mechanical Engineer"),
                _row("intern", "Software Engineer Intern", terms=["Summer 2026"]),
                _row("category", category="Marketing"),
                _row("keyword", "Accountant"),
                _row("degree", degrees=["PhD"]),
                _row("location", locations=["Austin, TX"]),
                _row("stale", date_posted=NOW - 40 * DAY, date_updated=NOW - 40 * DAY),
                _row("inactive", "Senior Engineer", active=False),
            ], "feed")
            with store.connect() as conn:
                conn.execute("UPDATE postings SET first_seen_at = '2000-01-01' "
                             "WHERE id = 'kept'")
                conn.execute("INSERT INTO postings (id, title, active, visible, "
                             "first_seen_at) VALUES ('ancient', 'Senior', 1, 1, '2000-01-01')")
            self.assertEqual(insights.why_nothing(settings.get(), 30), [
                {"rule": "exclude", "dropped": 2}, {"rule": "field exclude", "dropped": 1},
                {"rule": "intern term", "dropped": 1}, {"rule": "category", "dropped": 1},
                {"rule": "title keyword", "dropped": 1}, {"rule": "degree", "dropped": 1},
                {"rule": "location", "dropped": 1}, {"rule": "recent", "dropped": 1}])


class SuggestedCompaniesTest(unittest.TestCase):
    def test_companies_with_three_strong_groups_and_no_enabled_board(self):
        watchlist = [{"kind": "greenhouse", "location": "stripe"},
                     {"kind": "lever", "location": "palantir", "enabled": False}]
        with temp_home(watchlist=watchlist):
            rows, scores = [], []
            for company, fits in (("Jane Street", (95, 90, 85, 50)), ("Stripe", (90, 90, 90)),
                                  ("Palantir", (85, 82, 81)), ("Acme", (99, 99)),
                                  ("Old Co", (90, 90, 90))):
                for n, fit in enumerate(fits):
                    posting_id = f"{company}-{n}"
                    posted = NOW - 90 * DAY if company == "Old Co" else NOW
                    rows.append(_row(posting_id, f"Engineer {n}", company,
                                     date_posted=posted, date_updated=posted))
                    scores.append({"id": posting_id, "fit": fit, "tier": 60 + n})
            rows.append(_row("dup", "Engineer 0", "Jane Street", url="https://other.test/x"))
            store.upsert_postings(rows[:-1], "feed")
            store.upsert_postings(rows[-1:], "feed-two")
            store.save_scores(scores + [{"id": "dup", "fit": 95, "tier": 60}])
            self.assertEqual(insights.suggested_companies(), [
                {"company": "Jane Street", "postings": 3, "best_fit": 95, "mean_tier": 61,
                 "followed": False},
                {"company": "Palantir", "postings": 3, "best_fit": 85, "mean_tier": 61,
                 "followed": True}])
            self.assertEqual(len(insights.suggested_companies(limit=1)), 1)


class StatsTest(unittest.TestCase):
    def setUp(self):
        self.enterContext(temp_home())
        store.upsert_postings([_row(i, company=i) for i in "abcd"], "feed")
        store.upsert_postings([_row("e", company="e")], "board")
        self.clock = datetime.datetime.now() - datetime.timedelta(days=30)

    def _move(self, posting_id, status, days):
        at = (self.clock + datetime.timedelta(days=days)).isoformat(timespec="seconds")
        with mock.patch.object(store, "_now", lambda: at):
            store.set_status(posting_id, status)

    def test_funnel_response_times_weeks_and_sources(self):
        self._move("a", "saved", 0)
        self._move("a", "applied", 1)
        self._move("a", "interviewing", 5)
        self._move("a", "offer", 20)
        self._move("b", "applied", 2)
        self._move("b", "rejected", 4)
        self._move("c", "applied", 3)
        self._move("d", "saved", 3)
        self._move("e", "applied", 3)
        self._move("e", "withdrawn", 13)
        found = insights.stats()
        self.assertEqual(found["funnel"], {"saved": 2, "applied": 4, "interviewing": 1,
                                           "offer": 1, "rejected": 1, "withdrawn": 1})
        self.assertEqual(found["response_days"], {"median": 4.0, "p75": 7.0, "n": 3})
        self.assertEqual(len(found["weekly"]), insights.STATS_WEEKS)
        self.assertEqual(sum(w["applied"] for w in found["weekly"]), 4)
        self.assertEqual(sum(w["interviewing"] for w in found["weekly"]), 1)
        self.assertEqual(sum(w["offers"] for w in found["weekly"]), 1)
        self.assertEqual(datetime.date.fromisoformat(found["weekly"][0]["week"]).weekday(), 0)
        self.assertEqual(found["by_source"], [
            {"source": "feed", "applied": 3, "interviewing": 1, "offers": 1},
            {"source": "board", "applied": 1, "interviewing": 0, "offers": 0}])

    def test_no_applications(self):
        found = insights.stats()
        self.assertEqual(set(found["funnel"].values()), {0})
        self.assertEqual(found["response_days"], {"median": None, "p75": None, "n": 0})
        self.assertEqual(found["by_source"], [])


class RoutesTest(unittest.TestCase):
    def setUp(self):
        self.enterContext(temp_home())
        store.upsert_postings([_row("a"), _row("b", company="Beta")], "feed")
        app = FastAPI()
        app.include_router(insights_routes.router)
        self.client = self.enterContext(TestClient(app))
        self.pending, unsubscribe = events.queue_subscriber()
        self.addCleanup(unsubscribe)

    def next_event(self, kind):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                event = self.pending.get(timeout=0.1)
            except queue.Empty:
                continue
            if event.kind == kind:
                return event
        self.fail(f"no {kind} event")

    def test_feedback_is_set_changed_and_cleared(self):
        response = self.client.put("/api/jobs/a/feedback",
                                   json={"verdict": "down", "reason": "role", "note": "field"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual({k: v for k, v in response.json().items() if k != "created_at"},
                         {"verdict": "down", "reason": "role", "note": "field"})
        self.assertEqual(self.next_event("feedback_changed").data,
                         {"id": "a", "verdict": "down"})
        self.assertEqual(store.get_posting("a")["feedback"],
                         {"verdict": "down", "reason": "role"})
        self.client.put("/api/jobs/b/feedback", json={"verdict": "up"})
        self.assertEqual(self.client.get("/api/feedback/stats").json(),
                         {"up": 1, "down": 1, "total": 2})
        self.assertEqual(self.next_event("feedback_changed").data, {"id": "b", "verdict": "up"})
        self.assertEqual(self.client.delete("/api/jobs/a/feedback").status_code, 200)
        self.assertEqual(self.next_event("feedback_changed").data, {"id": "a", "verdict": None})
        self.assertIsNone(store.get_posting("a")["feedback"])

    def test_bad_feedback(self):
        self.assertEqual(self.client.put("/api/jobs/nope/feedback",
                                         json={"verdict": "up"}).status_code, 404)
        self.assertEqual(self.client.delete("/api/jobs/nope/feedback").status_code, 404)
        for body in ({"verdict": "meh"}, {"verdict": "up", "reason": "vibes"}):
            with self.subTest(body=body):
                self.assertEqual(self.client.put("/api/jobs/a/feedback",
                                                 json=body).status_code, 422)
        self.assertIsNone(store.feedback("a"))

    def test_facets_take_the_list_filters(self):
        body = self.client.get("/api/jobs/facets", params={"source": "other"}).json()
        self.assertEqual(body["source"], {"feed": 2})
        self.assertEqual(body["category"], {})
        self.assertEqual(self.client.get("/api/jobs/facets",
                                         params={"sponsorship": "maybe"}).status_code, 422)

    def test_insight_routes(self):
        self.assertEqual(self.client.get("/api/insights/skills").json(), [])
        self.assertEqual(self.client.get("/api/insights/companies").json(), [])
        self.assertEqual([r["rule"] for r in self.client.get("/api/insights/why").json()],
                         [name for name, _ in fetch.RULES] + ["recent"])
        self.assertEqual(self.client.get("/api/insights/stats").json()["funnel"]["applied"], 0)
        store.record_call("rank", "sonnet", 100, 10)
        self.assertEqual(self.client.get("/api/insights/cost", params={"days": 7}).json(),
                         {"rank": 1, "summary": 0, "onboard": 0, "total": 1,
                          "prompt_chars": 100})

    def test_a_link_check_runs_in_the_background(self):
        response = self.client.post("/api/insights/links")
        self.assertEqual((response.status_code, response.json()), (202, {"started": True}))
        self.assertEqual(self.next_event("links_done").data, {"checked": 0, "closed": 0})

    def test_a_link_check_is_refused_while_another_runs(self):
        with insights.LINK_LOCK:
            self.assertEqual(self.client.post("/api/insights/links").status_code, 409)


if __name__ == "__main__":
    unittest.main()
