"""The web API, through FastAPI's TestClient against a seeded temp home."""
import asyncio
import contextlib
import dataclasses
import io
import os
import posixpath
import queue
import re
import shutil
import subprocess
import tempfile
import threading
import time
import tomllib
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import httpx
import sys
from fastapi.testclient import TestClient
from helpers import app_client, temp_home, user_home
from ai.test_onboard import PROFILE, SUGGESTIONS, envelope

from cairn import cli, server, sources, store
from cairn.ai import claude, onboard
from cairn.core import events, paths, settings
from cairn.jobs import descriptions, fetch, logos, pipeline
from cairn.system import doctor, schedule
from cairn.core import secrets
from cairn.system import backup
from cairn.server import background
from cairn.server import onboard as onboard_routes
from cairn.server import runs as run_routes
from cairn.server import security
from cairn.server import status as status_routes
from cairn.server import system
from cairn.server.app import create_app

NOW = time.time()
DAY = 86400


def _row(posting_id, title="Software Engineer", company="Acme", **fields):
    row = {"id": posting_id, "company_name": company, "title": title,
           "url": f"https://jobs.test/{posting_id}", "locations": ["Seattle, WA"],
           "category": "Software", "active": True, "is_visible": True,
           "date_posted": NOW, "date_updated": NOW}
    row.update(fields)
    return row


def _seed():
    store.upsert_postings([
        _row("alpha", company="Alpha", locations=["New York, NY"], terms=["Winter 2027"]),
        _row("beta", company="Beta", category="Quant", title="Quant Developer"),
        _row("gamma", company="Gamma", date_posted=NOW - 40 * DAY,
             date_updated=NOW - 40 * DAY),
        _row("delta", company="Delta", active=False),
        _row("senior", company="Epsilon", title="Senior Software Engineer"),
    ], "feed-one")
    store.upsert_postings([_row("zeta", company="Zeta")], "feed-two")
    store.save_scores([
        {"id": "alpha", "fit": 90, "tier": 80, "below_floor": False, "fit_reason": "strong",
         "tier_reason": "top firm"},
        {"id": "beta", "fit": 70, "tier": 40, "below_floor": True, "fit_reason": "quant role"},
        {"id": "zeta", "fit": 50, "tier": 60, "below_floor": False},
    ])
    store.set_status("beta", "applied", "referral")
    store.save_description("alpha", "text", "html", None, "TECH: Kafka, Rust")
    store.mark_seen(["alpha"])


LOCAL = "http://127.0.0.1"


class ServerTestCase(unittest.TestCase):
    def setUp(self):
        self.home = self.enterContext(temp_home())
        _seed()
        self.client = app_client(self)

    def patch(self, module, name, value):
        self.addCleanup(setattr, module, name, getattr(module, name))
        setattr(module, name, value)

    def ids(self, **params):
        response = self.client.get("/api/jobs", params=params)
        self.assertEqual(response.status_code, 200, response.text)
        return [row["id"] for row in response.json()["rows"]]

    def next_event(self, pending, kind):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                event = pending.get(timeout=0.1)
            except queue.Empty:
                continue
            if event.kind == kind:
                return event
        self.fail(f"no {kind} event")


class JobsTest(ServerTestCase):
    def test_default_list_is_relevant_active_and_ranked(self):
        body = self.client.get("/api/jobs").json()
        self.assertEqual([r["id"] for r in body["rows"]], ["alpha", "zeta", "beta", "gamma"])
        self.assertEqual(body["total"], 4)
        alpha = body["rows"][0]
        self.assertEqual(alpha, {
            "id": "alpha", "company": "Alpha", "title": "Software Engineer",
            "url": "https://jobs.test/alpha", "locations": ["New York, NY"],
            "category": "Software", "terms": ["Winter 2027"], "source": "feed-one",
            "posted_at": NOW, "fit": 90,
            "tier": 80, "fit_reason": "strong", "tier_reason": "top firm",
            "below_floor": False, "status": None, "note": None,
            "applied_at": None, "updated_at": None, "seen": True, "run_id": None,
            "applied_before": None, "timing": None, "starts_before_graduation": False,
            "logo_domain": None, "feedback": None, "salary": None, "sponsorship": None,
            "closed": False, "also_on": []})
        self.assertFalse(body["rows"][1]["seen"])

    def test_rows_and_detail_carry_the_stored_icon_domain(self):
        store.save_company(store.name_key("Alpha"), "alpha.example", "company_url")
        store.save_company(store.name_key("Zeta"), "zeta.example", "company_url")
        store.save_logo("alpha.example", self.home / "logos" / "alpha.example.png")
        store.save_logo("zeta.example", None, "not an image")
        rows = {r["id"]: r for r in self.client.get("/api/jobs").json()["rows"]}
        self.assertEqual(rows["alpha"]["logo_domain"], "alpha.example")
        self.assertIsNone(rows["zeta"]["logo_domain"])
        self.assertIsNone(rows["gamma"]["logo_domain"])
        detail = self.client.get("/api/jobs/alpha").json()
        self.assertEqual(detail["logo_domain"], "alpha.example")

    def test_rows_carry_timing_and_applied_before(self):
        store.set_keywords("alpha", "TECH: Go\nDOMAIN: infra\nMUST: BS\nSIGNALS: speed\n"
                                    "TIMING: start summer 2026")
        store.upsert_postings([_row("beta2", company="beta", title="Software Engineer")],
                              "feed-one")
        settings.use(dataclasses.replace(settings.get(), graduation_year=2027))
        rows = {r["id"]: r for r in self.client.get("/api/jobs").json()["rows"]}
        self.assertEqual(rows["alpha"]["timing"], "start summer 2026")
        self.assertTrue(rows["alpha"]["starts_before_graduation"])
        self.assertEqual(rows["beta2"]["applied_before"],
                         store.application("beta")["applied_at"][:10])
        self.assertIsNone(rows["alpha"]["applied_before"])

    def test_run_source_and_category_filters(self):
        run_id = store.start_run("log")
        store.save_scores([{"id": "gamma", "fit": 61, "tier": 70, "below_floor": False}],
                          run_id)
        self.assertEqual(self.ids(run_id=run_id), ["gamma"])
        self.assertEqual(self.ids(run_id=run_id + 1), [])
        self.assertEqual(self.ids(source=["feed-one", "feed-two"]),
                         ["alpha", "gamma", "zeta", "beta"])
        self.assertEqual(self.ids(category=["Quant", "Software"], source="feed-one"),
                         ["alpha", "gamma", "beta"])

    def test_each_filter(self):
        cases = [
            ({"q": "kafka"}, ["alpha"]),
            ({"q": "beta"}, ["beta"]),
            ({"relevant_only": "false"}, ["alpha", "zeta", "beta", "senior", "gamma"]),
            ({"active_only": "false"}, ["alpha", "zeta", "beta", "gamma"]),
            ({"relevant_only": "false", "active_only": "false", "sort": "company"},
             ["alpha", "beta", "delta", "senior", "gamma", "zeta"]),
            ({"fit_min": 60}, ["alpha", "beta"]),
            ({"tier_min": 60}, ["alpha", "zeta"]),
            ({"status": "applied"}, ["beta"]),
            ({"status": "none"}, ["alpha", "zeta", "gamma"]),
            ({"status": ["applied", "none"]}, ["alpha", "zeta", "beta", "gamma"]),
            ({"status": "saved"}, []),
            ({"category": "Quant"}, ["beta"]),
            ({"location": "new york"}, ["alpha"]),
            ({"source": "feed-two"}, ["zeta"]),
            ({"posted_within_days": 7}, ["alpha", "zeta", "beta"]),
            ({"sort": "newest", "posted_within_days": 7}, ["alpha", "beta", "zeta"]),
        ]
        for params, expected in cases:
            with self.subTest(params=params):
                self.assertEqual(self.ids(**params), expected)

    def test_paging(self):
        page = self.client.get("/api/jobs", params={"limit": 2, "offset": 1}).json()
        self.assertEqual([r["id"] for r in page["rows"]], ["zeta", "beta"])
        self.assertEqual(page["total"], 4)

    def test_bad_query_parameters_are_400(self):
        for params in ({"limit": 0}, {"limit": 201}, {"offset": -1}, {"sort": "fit"},
                       {"fit_min": "high"}, {"status": "ghosted"}):
            with self.subTest(params=params):
                response = self.client.get("/api/jobs", params=params)
                self.assertEqual(response.status_code, 400)
                self.assertIn("error", response.json())

    def test_detail(self):
        job = self.client.get("/api/jobs/alpha").json()
        self.assertEqual((job["fit_reason"], job["tier_reason"]), ("strong", "top firm"))
        self.assertEqual(job["keywords"], "TECH: Kafka, Rust")
        self.assertIsNone(job["timing"])
        self.assertFalse(job["starts_before_graduation"])
        self.assertEqual(job["description"]["keywords"], "TECH: Kafka, Rust")
        self.assertIsNone(job["description"]["error"])
        self.assertIsNotNone(job["description"]["fetched_at"])
        beta = self.client.get("/api/jobs/beta").json()
        self.assertEqual((beta["status"], beta["note"]), ("applied", "referral"))
        self.assertEqual([e["status"] for e in beta["events"]], ["applied"])
        self.assertEqual(job["events"], [])
        self.assertIsNone(self.client.get("/api/jobs/zeta").json()["description"])
        self.assertIsNone(self.client.get("/api/jobs/zeta").json()["keywords"])

    def test_detail_flags_a_start_before_graduation(self):
        settings.use(dataclasses.replace(settings.get(), graduation_year=2027))
        store.set_keywords("zeta", "TECH: Go\nDOMAIN: x\nMUST: y\nSIGNALS: z\n"
                                   "TIMING: summer 2026 start")
        job = self.client.get("/api/jobs/zeta").json()
        self.assertEqual(job["timing"], "summer 2026 start")
        self.assertTrue(job["starts_before_graduation"])

    def test_detail_says_whether_it_matches_the_preferences(self):
        self.assertTrue(self.client.get("/api/jobs/alpha").json()["relevant"])
        self.assertFalse(self.client.get("/api/jobs/senior").json()["relevant"])
        self.assertFalse(self.client.get("/api/jobs/delta").json()["relevant"])

    def test_a_stored_url_that_is_not_http_is_never_returned(self):
        # rows stored before ingest checked the scheme can still hold one
        with store.connect() as conn:
            conn.execute("UPDATE postings SET url = 'javascript:alert(1)' WHERE id = 'alpha'")
        self.assertIsNone(self.client.get("/api/jobs/alpha").json()["url"])
        rows = self.client.get("/api/jobs", params={"q": "Alpha"}).json()["rows"]
        self.assertEqual([row["url"] for row in rows], [None])
        self.assertEqual(self.client.get("/api/jobs/beta").json()["url"],
                         "https://jobs.test/beta")

    def test_unknown_detail_is_404(self):
        response = self.client.get("/api/jobs/nope")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json(), {"error": "unknown posting id: nope"})


class ApplicationTest(ServerTestCase):
    def setUp(self):
        super().setUp()
        self.pending, unsubscribe = events.queue_subscriber()
        self.addCleanup(unsubscribe)

    def put(self, posting_id, body):
        return self.client.put(f"/api/jobs/{posting_id}/application", json=body)

    def test_put_a_status_then_another(self):
        response = self.put("zeta", {"status": "saved"})
        self.assertEqual(response.status_code, 200, response.text)
        job = response.json()
        self.assertEqual((job["id"], job["status"], job["applied_at"]), ("zeta", "saved", None))
        self.assertIsNotNone(job["updated_at"])
        self.assertEqual(self.next_event(self.pending, "application_changed").data,
                         {"id": "zeta", "status": "saved"})

        job = self.put("zeta", {"status": "applied", "note": "online"}).json()
        self.assertEqual((job["status"], job["note"]), ("applied", "online"))
        self.assertIsNotNone(job["applied_at"])
        self.assertEqual([e["status"] for e in job["events"]], ["saved", "applied"])

    def test_put_a_note_alone(self):
        job = self.put("beta", {"note": "call back Friday"}).json()
        self.assertEqual((job["status"], job["note"]), ("applied", "call back Friday"))
        self.assertEqual(len(job["events"]), 1)
        self.assertEqual(self.next_event(self.pending, "application_changed").data,
                         {"id": "beta", "status": "applied"})

    def test_a_note_without_a_status_to_attach_to_is_409(self):
        response = self.put("zeta", {"note": "hello"})
        self.assertEqual(response.status_code, 409)
        self.assertIsNone(store.application("zeta"))

    def test_a_body_without_status_or_note_is_400(self):
        for body in ({}, {"status": None, "note": None}):
            with self.subTest(body=body):
                response = self.put("zeta", body)
                self.assertEqual(response.status_code, 400)
                self.assertIn("give a status, a note, or both", response.json()["error"])

    def test_an_unknown_status_or_field_is_400(self):
        response = self.put("zeta", {"status": "ghosted"})
        self.assertEqual(response.status_code, 400)
        for status in store.STATUSES:
            self.assertIn(f"'{status}'", response.json()["error"])
        self.assertEqual(self.put("zeta", {"status": "saved", "extra": 1}).status_code, 400)
        self.assertIsNone(store.application("zeta"))

    def test_delete_clears_the_status_and_its_history(self):
        response = self.client.delete("/api/jobs/beta/application")
        self.assertEqual(response.status_code, 200)
        job = response.json()
        self.assertEqual((job["status"], job["applied_at"], job["note"], job["events"]),
                         (None, None, None, []))
        self.assertIsNone(store.application("beta"))
        self.assertEqual(self.next_event(self.pending, "application_changed").data,
                         {"id": "beta", "status": None})

    def test_unknown_id_is_404(self):
        self.assertEqual(self.put("nope", {"status": "saved"}).status_code, 404)
        self.assertEqual(self.client.delete("/api/jobs/nope/application").status_code, 404)

    def test_listing_applications(self):
        self.put("alpha", {"status": "saved"})
        self.put("zeta", {"status": "interviewing"})
        body = self.client.get("/api/applications").json()
        self.assertEqual({r["id"] for r in body["rows"]}, {"alpha", "beta", "zeta"})
        self.assertEqual(body["counts"], {"saved": 1, "applied": 1, "interviewing": 1,
                                          "offer": 0, "rejected": 0, "withdrawn": 0,
                                          "passed": 0})
        alpha = next(r for r in body["rows"] if r["id"] == "alpha")
        self.assertEqual((alpha["company"], alpha["fit"], alpha["url"], alpha["locations"]),
                         ("Alpha", 90, "https://jobs.test/alpha", ["New York, NY"]))

        filtered = self.client.get("/api/applications",
                                   params={"status": ["saved", "interviewing"]}).json()
        self.assertEqual({r["id"] for r in filtered["rows"]}, {"alpha", "zeta"})
        self.assertEqual(filtered["counts"]["applied"], 1)
        self.assertEqual(self.client.get("/api/applications",
                                         params={"status": "none"}).status_code, 400)

    def test_application_rows_carry_the_stored_icon_domain(self):
        self.put("alpha", {"status": "saved"})
        store.save_company(store.name_key("Alpha"), "alpha.example", "company_url")
        store.save_company(store.name_key("Beta"), "beta.example", "company_url")
        store.save_logo("alpha.example", self.home / "logos" / "alpha.example.png")
        store.save_logo("beta.example", None, "not an image")
        rows = {r["id"]: r for r in self.client.get("/api/applications").json()["rows"]}
        self.assertEqual(rows["alpha"]["logo_domain"], "alpha.example")
        self.assertIsNone(rows["beta"]["logo_domain"])

    def test_passed_postings_leave_the_default_list(self):
        self.put("zeta", {"status": "passed"})
        self.assertNotIn("zeta", self.ids())
        self.assertIn("zeta", self.ids(hide_passed="false"))
        self.assertEqual(self.ids(status="passed"), ["zeta"])
        self.patch(store.db, "now", lambda: "2099-01-01T00:00:00")
        self.put("gamma", {"status": "saved"})
        self.assertEqual(self.ids(sort="updated"), ["gamma", "beta", "alpha"])


class SummaryTest(ServerTestCase):
    def setUp(self):
        super().setUp()
        self.calls = []

        def keywords(job):
            self.calls.append(job["id"])
            return "TECH: Go\nDOMAIN: infra\nMUST: CS degree\nSIGNALS: scale"

        self.patch(descriptions, "keywords", keywords)
        self.pending, unsubscribe = events.queue_subscriber()
        self.addCleanup(unsubscribe)

    def test_starts_a_summary_and_its_events_go_out(self):
        response = self.client.post("/api/jobs/zeta/summary")
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json(), {"started": True})
        self.assertEqual(self.next_event(self.pending, "summary_start").data["id"], "zeta")
        done = self.next_event(self.pending, "summary_done").data
        self.assertEqual((done["id"], done["err"]), ("zeta", None))
        self.assertIn("TECH: Go", done["keywords"])
        self.assertEqual(self.calls, ["zeta"])

    def test_runs_while_a_run_holds_the_lock(self):
        with pipeline.run_lock():
            response = self.client.post("/api/jobs/zeta/summary")
            self.assertEqual(response.status_code, 202)
            self.next_event(self.pending, "summary_done")

    def test_unknown_id_is_404(self):
        response = self.client.post("/api/jobs/nope/summary")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json(), {"error": "unknown posting id: nope"})
        self.assertEqual(self.calls, [])

    def test_a_failure_is_one_warning_and_a_summary_done(self):
        def broken(job):
            raise RuntimeError("claude is down")

        self.patch(descriptions, "keywords", broken)
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            self.assertEqual(self.client.post("/api/jobs/zeta/summary").status_code, 202)
            done = self.next_event(self.pending, "summary_done")
            warning = self.next_event(self.pending, "warn")
        self.assertIn("claude is down", done.data["err"])
        self.assertIn("claude is down", warning.data["text"])
        self.assertEqual(stderr.getvalue(), "")


class ExplainTest(ServerTestCase):
    REPLY = '{"fit_reason": "Backend Go role fits the profile.", ' \
            '"tier_reason": "Mid-size firm below the profile\'s anchors."}'

    def setUp(self):
        super().setUp()
        paths.profile_md().write_text("Candidate profile: a new grad.", encoding="utf-8")
        settings.use(dataclasses.replace(settings.get(), llm_provider="ollama"))
        self.prompts = []

        def fake_run(prompt, model=None, timeout=240, tier=None):
            self.prompts.append(prompt)
            return self.REPLY, None

        self.patch(claude, "run", fake_run)

    def test_stores_both_reasons_and_counts_a_rank_call(self):
        response = self.client.post("/api/jobs/zeta/reasons")
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual((body["fit"], body["tier"], body["fit_reason"], body["tier_reason"]),
                         (50, 60, "Backend Go role fits the profile.",
                          "Mid-size firm below the profile's anchors."))
        self.assertIn("scored fit 50 and tier 60", self.prompts[0])
        self.assertIn("Candidate profile: a new grad.", self.prompts[0])
        self.assertEqual(store.call_counts()["rank"], 1)
        rows = {row["id"]: row for row in self.client.get("/api/jobs").json()["rows"]}
        self.assertEqual(rows["zeta"]["tier_reason"],
                         "Mid-size firm below the profile's anchors.")

    def test_no_provider_is_409_without_a_call(self):
        settings.use(dataclasses.replace(settings.get(), llm_provider="claude-code",
                                         claude_bin="/nonexistent/claude"))
        response = self.client.post("/api/jobs/zeta/reasons")
        self.assertEqual(response.status_code, 409)
        self.assertIn("no AI provider", response.json()["error"])
        self.assertEqual((self.prompts, store.call_counts()["total"]), ([], 0))

    def test_the_monthly_cap_is_429_without_a_call(self):
        settings.use(dataclasses.replace(settings.get(), monthly_call_cap=1))
        store.record_call("rank", "sonnet", 10, 10)
        response = self.client.post("/api/jobs/zeta/reasons")
        self.assertEqual(response.status_code, 429)
        self.assertIn("limit of 1 AI requests", response.json()["error"])
        self.assertEqual(self.prompts, [])
        self.assertIsNone(store.get_posting("zeta")["fit_reason"])

    def test_a_reply_without_reasons_is_502_and_still_counts(self):
        self.REPLY = '{"fit": 50}'
        response = self.client.post("/api/jobs/zeta/reasons")
        self.assertEqual(response.status_code, 502)
        self.assertIn("gave no reasons", response.json()["error"])
        self.assertEqual(store.call_counts()["rank"], 1)

    def test_an_unscored_or_unknown_posting_is_refused(self):
        self.assertEqual(self.client.post("/api/jobs/gamma/reasons").status_code, 409)
        self.assertEqual(self.client.post("/api/jobs/nope/reasons").status_code, 404)
        self.assertEqual(self.prompts, [])


class SettingsTest(ServerTestCase):
    def test_round_trip(self):
        body = self.client.get("/api/settings").json()
        self.assertEqual(body["fit_threshold"], 60)
        self.assertEqual(body["defaults"]["fit_threshold"], 60)
        self.assertEqual(body["path"], str(paths.config_file()))

        response = self.client.put("/api/settings", json={"fit_threshold": 70,
                                                          "location_allow": ["Remote"]})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["fit_threshold"], 70)
        with open(paths.config_file(), "rb") as f:
            saved = tomllib.load(f)
        self.assertEqual(saved, {"fit_threshold": 70, "location_allow": ["Remote"]})
        body = self.client.get("/api/settings").json()
        self.assertEqual((body["fit_threshold"], body["defaults"]["fit_threshold"]), (70, 60))

    def test_a_bad_key_or_type_is_400_and_writes_nothing(self):
        for payload, message in (({"fit_treshold": 70}, "unknown setting 'fit_treshold'"),
                                 ({"fit_threshold": "70"}, "'fit_threshold' should be")):
            with self.subTest(payload=payload):
                response = self.client.put("/api/settings", json=payload)
                self.assertEqual(response.status_code, 400)
                self.assertIn(message, response.json()["error"])
        self.assertFalse(paths.config_file().exists())

    def test_new_relevance_settings_recompute_the_stored_relevance(self):
        def stored():
            return {r[0] for r in store.connect().execute(
                "SELECT id FROM postings WHERE relevant = 1")}
        self.assertIn("alpha", stored())
        response = self.client.put("/api/settings", json={"title_keywords": ["no such role"]})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(stored(), set())
        self.assertEqual(self.ids(), [])

    def test_a_non_object_body_is_400(self):
        self.assertEqual(self.client.put("/api/settings", json=[1]).status_code, 400)

    def test_invalid_json_is_400(self):
        response = self.client.put("/api/settings", content=b"{not json",
                                   headers={"content-type": "application/json"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json(), {"error": "request body is not valid JSON"})


class WatchlistResolveTest(ServerTestCase):
    def test_a_board_url_resolves_without_probing(self):
        self.patch(sources.resolve, "_has_jobs", lambda kind, slug: self.fail("probed a URL"))
        response = self.client.post("/api/watchlist/resolve",
                                    json={"query": "https://jobs.lever.co/palantir"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(),
                         {"kind": "lever", "location": "palantir", "company": "Palantir"})

    def test_a_name_resolves_to_the_first_board_with_jobs(self):
        self.patch(sources.resolve, "_has_jobs", lambda kind, slug: kind == "ashby")
        response = self.client.post("/api/watchlist/resolve", json={"query": "Ramp "})
        self.assertEqual(response.json(),
                         {"kind": "ashby", "location": "ramp", "company": "Ramp"})

    def test_no_board_is_404_and_a_blank_query_is_400(self):
        self.patch(sources.resolve, "_has_jobs", lambda kind, slug: False)
        response = self.client.post("/api/watchlist/resolve", json={"query": "Nobody Inc"})
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["error"],
                         "Cairn couldn't find a careers page for Nobody Inc")
        for query in ("", "   ", "x" * 201):
            self.assertEqual(self.client.post("/api/watchlist/resolve",
                                              json={"query": query}).status_code, 400, query)


class FilesTest(ServerTestCase):
    def test_round_trip(self):
        for name, path in (("profile", paths.profile_md()),):
            with self.subTest(name=name):
                response = self.client.put(f"/api/files/{name}", json={"text": f"# {name}\n"})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(path.read_text(encoding="utf-8"), f"# {name}\n")
                self.assertEqual(self.client.get(f"/api/files/{name}").json(),
                                 {"name": name, "path": str(path), "text": f"# {name}\n"})
        self.assertEqual([p.name for p in self.home.iterdir() if p.name.startswith(".")], [])

    def test_other_names_are_404(self):
        for name in ("secret", "master", "template"):
            self.assertEqual(self.client.get(f"/api/files/{name}").status_code, 404)
        response = self.client.put("/api/files/secret", json={"text": "x"})
        self.assertEqual(response.status_code, 404)
        self.assertFalse((self.home / "secret").exists())


class RunTest(ServerTestCase):
    def setUp(self):
        super().setUp()
        self.release = threading.Event()
        self.addCleanup(self.release.set)

        self.run_threads = []

        def fake(opts, on_start=None):
            with pipeline.run_lock():
                store.connect()
                self.run_threads.append(threading.get_ident())
                on_start(41)
                self.release.wait(timeout=10)

        self.patch(pipeline, "run", fake)

    def test_starts_a_run_and_a_second_is_refused(self):
        response = self.client.post("/api/run", json={"limit": 3, "dry_run": True})
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json(), {"run_id": 41})
        self.assertEqual(self.client.get("/api/run/status").json(),
                         {"running": True, "icons": False, "pid": os.getpid(), "run": None})
        response = self.client.post("/api/run", json={})
        self.assertEqual(response.status_code, 409)
        self.assertIn("error", response.json())

    def test_refused_while_the_test_holds_the_lock(self):
        with pipeline.run_lock():
            self.assertEqual(self.client.post("/api/run").status_code, 409)

    def test_a_job_that_never_starts_is_a_500(self):
        self.patch(background, "START_TIMEOUT", 0.1)
        self.patch(pipeline, "run", lambda opts, on_start=None: self.release.wait(10))
        response = self.client.post("/api/run")
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.json(), {"error": "background job did not start"})

    def test_a_finished_job_closes_its_connections(self):
        self.assertEqual(self.client.post("/api/run").status_code, 202)
        (ident,) = self.run_threads
        self.assertIn(ident, [key[1] for key in store.db._connections])
        self.release.set()
        deadline = time.monotonic() + 5
        while ident in [key[1] for key in store.db._connections]:
            self.assertLess(time.monotonic(), deadline, "the job kept its connection")
            time.sleep(0.02)


class RunOverrideTest(ServerTestCase):
    """A run's --fit never leaks into the settings the server edits."""

    def test_a_settings_edit_during_a_run_keeps_only_its_own_keys(self):
        inside, release, finished = threading.Event(), threading.Event(), threading.Event()
        during = []
        self.addCleanup(release.set)

        def fake_recorded(opts, on_start):
            on_start(1)
            inside.set()
            release.wait(timeout=10)
            during.append(settings.get().fit_threshold)
            finished.set()

        self.patch(pipeline, "_recorded", fake_recorded)
        response = self.client.post("/api/run", json={"fit": 95})
        self.assertEqual(response.status_code, 202)
        self.assertTrue(inside.wait(5))

        body = self.client.get("/api/settings").json()
        self.assertEqual(body["fit_threshold"], 60)
        response = self.client.put("/api/settings", json={"notify_macos": True})
        self.assertEqual(response.status_code, 200)
        with open(paths.config_file(), "rb") as f:
            self.assertEqual(tomllib.load(f), {"notify_macos": True})

        release.set()
        self.assertTrue(finished.wait(5))
        self.assertEqual(during, [95])
        self.assertTrue(settings.get().notify_macos)
        self.assertEqual(settings.get().fit_threshold, 60)

    def test_status_when_idle(self):
        self.assertEqual(self.client.get("/api/run/status").json(),
                         {"running": False, "icons": False, "pid": None, "run": None})

    def test_a_first_run_is_asked_for_in_the_body(self):
        asked = []

        def fake(opts, on_start=None):
            asked.append(opts)
            on_start(7)

        self.patch(pipeline, "run", fake)
        self.assertEqual(self.client.post("/api/run", json={"first_run": True}).status_code, 202)
        self.assertEqual(self.client.post("/api/run", json={}).status_code, 202)
        self.assertEqual([opts.first_run for opts in asked], [True, False])

    def test_a_bad_limit_is_400(self):
        self.assertEqual(self.client.post("/api/run", json={"limit": 0}).status_code, 400)


class RunsTest(ServerTestCase):
    def test_list_and_log_slice(self):
        paths.run_log().write_text("before\nrun line 1\nrun line 2\nafter\n", encoding="utf-8",
                                   newline="\n")
        finished = store.start_run(str(paths.run_log()))
        store.finish_run(finished, "ok", {"ranked": 2}, log_start=7, log_end=29)
        running = store.start_run(str(paths.run_log()))

        runs = self.client.get("/api/runs", params={"limit": 20}).json()
        self.assertEqual([r["id"] for r in runs], [running, finished])
        self.assertEqual(runs[1]["counts"], {"ranked": 2})

        response = self.client.get(f"/api/runs/{finished}/log")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.headers["content-type"].startswith("text/plain"))
        self.assertEqual(response.text, "run line 1\nrun line 2\n")
        self.assertEqual(self.client.get(f"/api/runs/{running}/log").text, "")
        self.assertEqual(self.client.get("/api/runs/999/log").status_code, 404)

    def test_each_run_counts_what_it_ranked_that_the_jobs_list_shows(self):
        run_id = store.start_run(str(paths.run_log()))
        store.save_scores([{"id": "alpha", "fit": 90}, {"id": "senior", "fit": 40},
                           {"id": "delta", "fit": 30}], run_id)
        store.finish_run(run_id, "ok", {"ranked": 3})
        run = self.client.get("/api/runs").json()[0]
        self.assertEqual((run["counts"]["ranked"], run["relevant_ranked"]), (3, 1))

    def test_one_run(self):
        run_id = store.start_run(str(paths.run_log()))
        store.finish_run(run_id, "ok", {"ranked": 2})
        response = self.client.get(f"/api/runs/{run_id}")
        self.assertEqual(response.status_code, 200)
        self.assertEqual((response.json()["id"], response.json()["status"],
                          response.json()["counts"]), (run_id, "ok", {"ranked": 2}))
        self.assertEqual(self.client.get("/api/runs/999").status_code, 404)

    def test_a_run_in_progress_has_its_options_and_its_log_so_far(self):
        earlier = store.start_run(str(paths.run_log()))
        store.finish_run(earlier, "ok", {}, log_start=0, log_end=7)
        run_id = store.start_run(str(paths.run_log()))
        start = f"2026-09-25 07:00:00  run {run_id} started  limit=5  fit=None  dry_run=True\n"
        paths.run_log().write_text(f"before\n{start}2026-09-25 07:00:01  == fetch ==\n",
                                   encoding="utf-8", newline="\n")
        with pipeline.run_lock():
            status = self.client.get("/api/run/status").json()
            log = self.client.get(f"/api/runs/{run_id}/log").text
        self.assertTrue(status["running"])
        self.assertEqual(status["run"]["id"], run_id)
        self.assertEqual(status["run"]["options"], {"limit": 5, "fit": None, "dry_run": True})
        self.assertIsNotNone(status["run"]["started_at"])
        self.assertEqual(log, f"{start}2026-09-25 07:00:01  == fetch ==\n")

    def test_a_log_is_read_from_run_log_alone_and_cut_to_its_end(self):
        secret = self.home / "secret.txt"
        secret.write_text("the resume\n", encoding="utf-8")
        paths.run_log().write_text("0123456789abcdefghij\n", encoding="utf-8", newline="\n")
        run_id = store.start_run(str(secret))
        store.finish_run(run_id, "failed", {}, log_start=0, log_end=21)
        crafted = store.start_run(str(secret))
        store.finish_run(crafted, "failed", {}, log_start=-5, log_end=3)
        self.patch(run_routes, "MAX_RUN_LOG_BYTES", 11)
        self.assertEqual(self.client.get(f"/api/runs/{run_id}/log").text, "abcdefghij\n")
        self.assertEqual(self.client.get(f"/api/runs/{crafted}/log").text, "012")
        running = store.start_run(str(secret))
        secret.write_text(f"x  run {running} started  limit=1\n", encoding="utf-8")
        with pipeline.run_lock():
            status = self.client.get("/api/run/status").json()
        self.assertEqual(status["run"]["options"], {})
        self.assertEqual(self.client.get(f"/api/runs/{running}/log").text, "")

    def test_a_held_lock_without_a_running_run_names_no_run(self):
        store.finish_run(store.start_run(), "ok", {})
        with pipeline.run_lock():
            status = self.client.get("/api/run/status").json()
        self.assertEqual((status["running"], status["run"]), (True, None))

    def test_a_marker_left_by_a_dead_icon_fetch_holds_nothing(self):
        (self.home / "icons.pid").write_text(str(os.getpid() + 1), encoding="utf-8")
        with pipeline.run_lock():
            status = self.client.get("/api/run/status").json()
        self.assertEqual((status["running"], status["icons"]), (True, False))


class StatusTest(ServerTestCase):
    def test_status(self):
        body = self.client.get("/api/status").json()
        self.assertEqual(body["postings"], 6)
        self.assertEqual(body["applied"], 1)
        self.assertEqual(body["home"], str(self.home))
        self.assertEqual(body["database"], str(paths.db_file()))
        self.assertRegex(body["version"], r"^\d+\.\d+")
        self.assertEqual(body["kinds"], list(sources.KINDS))
        self.assertEqual(body["schema_version"], store.SCHEMA_VERSION)
        self.assertEqual(body["applications"]["applied"], 1)
        self.assertEqual(body["applications"]["saved"], 0)
        self.assertIsNone(body["latest_run"])
        run_id = store.start_run()
        store.save_scores([{"id": "zeta", "fit": 55, "tier": 60}], run_id)
        latest = self.client.get("/api/status").json()["latest_run"]
        self.assertEqual((latest["id"], latest["status"], latest["ranked"]),
                         (run_id, "running", 1))
        self.assertNotIn("latest_report", body)

    def test_doctor(self):
        self.patch(doctor.shutil, "which", lambda name, **kwargs: None)
        self.patch(schedule, "plist_path", lambda: self.home / "absent.plist")
        self.patch(schedule, "autostart_plist_path", lambda: self.home / "absent-ui.plist")
        self.patch(schedule.subprocess, "run",
                   lambda cmd, **kwargs: SimpleNamespace(returncode=0, stdout="", stderr=""))
        self.assertEqual(self.client.get("/api/doctor").status_code, 404)
        checks = self.client.post("/api/doctor").json()["checks"]
        self.assertEqual([c["name"] for c in checks],
                         ["python", "claude", "home", "schedule", "start at login",
                          "pdftotext"])
        by_name = {c["name"]: c for c in checks}
        self.assertTrue(by_name["python"]["ok"])
        self.assertFalse(by_name["claude"]["ok"])
        self.assertIn("claude.com/claude-code", by_name["claude"]["fix"])
        self.assertFalse(by_name["home"]["ok"])
        self.assertEqual(by_name["home"]["fix"], "finish setup in the app")

    @unittest.skipIf(sys.platform == "win32", "launchd is macOS only")
    def test_schedule(self):
        plist = self.home / "app.cairn.daily.plist"
        self.patch(schedule, "plist_path", lambda: plist)
        self.patch(schedule.subprocess, "run",
                   lambda cmd, **kwargs: SimpleNamespace(returncode=0, stdout="", stderr=""))
        body = self.client.get("/api/schedule").json()
        self.assertEqual(body, {"installed": False, "loaded": False, "plist": str(plist),
                                "program": None, "hour": None, "minute": None,
                                "hourly": False, "error": ""})

    def test_index_page_and_its_assets(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Cairn", response.text)
        self.assertIn("/app.js", response.text)
        self.assertIn("/css/tokens.css", response.text)
        for url, kind in (("/app.js", "javascript"), ("/css/tokens.css", "text/css")):
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 200)
                self.assertIn(kind, response.headers["content-type"])
                self.assertTrue(response.text.strip())

    def test_the_page_and_its_assets_are_revalidated_on_every_load(self):
        for url in ("/", "/app.js", "/views/jobs.js", "/css/tokens.css",
                    "/fonts/ChicagoFLF.woff2"):
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.headers["cache-control"], "no-cache")
        etag = self.client.get("/app.js").headers["etag"]
        self.assertEqual(self.client.get("/app.js", headers={"if-none-match": etag})
                         .status_code, 304)

    def test_every_module_stylesheet_and_font_the_page_loads_is_served(self):
        kinds = {".html": "text/html", ".js": "text/javascript", ".css": "text/css",
                 ".woff2": "font/woff2"}
        reference = re.compile(
            r'(?:from |import\(|url\(|src=|href=)"(\.{0,2}/[\w./-]+\.(?:js|css|woff2))"')
        pending, loaded = ["/index.html"], set()
        while pending:
            url = pending.pop()
            if url in loaded:
                continue
            loaded.add(url)
            response = self.client.get(url)
            with self.subTest(url=url):
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.headers["content-type"].split(";")[0],
                                 kinds[os.path.splitext(url)[1]])
            if not url.endswith(".woff2"):
                pending += [posixpath.normpath(posixpath.join(posixpath.dirname(url), ref))
                            for ref in reference.findall(response.text)]
        for expected in ("/app.js", "/views/jobs.js", "/views/detail.js", "/lib/api.js",
                         "/css/views/jobs.css", "/fonts/ChicagoFLF.woff2",
                         "/fonts/ibm-plex-sans-latin-400-normal.woff2",
                         "/fonts/ibm-plex-mono-latin-500-normal.woff2"):
            self.assertIn(expected, loaded)

    def test_an_unexpected_error_is_a_500_without_a_traceback(self):
        def broken():
            raise RuntimeError("disk on fire")

        self.patch(store, "counts", broken)
        client = app_client(self, raise_server_exceptions=False)
        response = client.get("/api/status")
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.json(), {"error": "RuntimeError: disk on fire"})


class LogoTest(ServerTestCase):
    def test_a_home_that_moved_still_serves_its_icons(self):
        path = logos.path_for("acme.example", "png")
        path.parent.mkdir(parents=True)
        path.write_bytes(b"\x89PNG\r\n\x1a\n")
        store.save_logo("acme.example", path, source="site")
        store.close()
        moved = Path(self.enterContext(tempfile.TemporaryDirectory())) / "home"
        shutil.copytree(self.home, moved)
        shutil.rmtree(self.home / "logos")
        self.enterContext(mock.patch.dict(os.environ, {"CAIRN_HOME": str(moved)}))
        self.addCleanup(store.close)
        response = self.client.get("/api/logos/acme.example")
        self.assertEqual((response.status_code, response.content),
                         (200, b"\x89PNG\r\n\x1a\n"))

    def test_a_stored_logo_is_served_with_its_type_and_headers(self):
        path = logos.path_for("acme.example", "svg")
        path.parent.mkdir(parents=True)
        path.write_bytes(b"<svg xmlns='http://www.w3.org/2000/svg'/>")
        store.save_logo("acme.example", path)
        response = self.client.get("/api/logos/acme.example")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, path.read_bytes())
        self.assertEqual(response.headers["content-type"], "image/svg+xml")
        self.assertEqual(response.headers["cache-control"], "max-age=86400")
        self.assertEqual(response.headers["content-security-policy"],
                         "default-src 'none'; style-src 'unsafe-inline'")
        self.assertEqual(response.headers["x-content-type-options"], "nosniff")

    def test_an_unknown_domain_is_404(self):
        store.save_logo("broken.example", None, "not an image")
        for domain in ("nobody.example", "broken.example"):
            self.assertEqual(self.client.get(f"/api/logos/{domain}").status_code, 404, domain)

    def test_a_malformed_domain_is_404_without_a_lookup(self):
        self.patch(logos, "logo_file", lambda domain: self.fail(f"looked up {domain!r}"))
        for domain in ("..", "..%2Fx", "%2E%2E%2Fpipeline.db", "Acme.example", "a_b.example",
                       "acme", "x" * 250 + ".com"):
            response = self.client.get(f"/api/logos/{domain}")
            self.assertEqual(response.status_code, 404, domain)
            self.assertNotIn(b"SQLite", response.content)


class IconsTest(ServerTestCase):
    def setUp(self):
        super().setUp()
        self.release = threading.Event()
        self.addCleanup(self.release.set)
        self.pending, unsubscribe = events.queue_subscriber()
        self.addCleanup(unsubscribe)

        def fetch_all(progress=None, limit=None):
            progress(3, 2)
            self.release.wait(10)
            return logos.Job(1, self.offline)
        self.offline = False
        self.patch(logos, "fetch_all", fetch_all)

    def _icons_status(self):
        return self.client.get("/api/status").json()["icons"]

    def _wait_until_idle(self):
        deadline = time.monotonic() + 5
        while self._icons_status()["running"]:
            self.assertLess(time.monotonic(), deadline, "the icon fetch never ended")
            time.sleep(0.02)

    def test_a_fetch_reports_each_pass_and_its_end_and_a_second_is_refused(self):
        self.assertEqual(self.client.post("/api/icons").status_code, 202)
        self.assertEqual(self.next_event(self.pending, "icons_progress").data,
                         {"done": 3, "remaining": 2})
        self.assertTrue(self._icons_status()["running"])
        response = self.client.post("/api/icons")
        self.assertEqual((response.status_code, response.json()),
                         (409, {"error": "a company icon fetch is already running"}))
        self.assertEqual(self.client.get("/api/run/status").json(),
                         {"running": False, "icons": True, "pid": None, "run": None})
        self.release.set()
        self.assertEqual(self.next_event(self.pending, "icons_done").data,
                         {"fetched": 1, "error": None})
        self._wait_until_idle()

    def test_a_run_and_seeding_go_ahead_while_icons_are_fetched(self):
        self.patch(fetch, "seed", lambda: 4)
        self.assertEqual(self.client.post("/api/icons").status_code, 202)
        self.next_event(self.pending, "icons_progress")
        self.assertEqual(self.client.post("/api/seed").json(), {"seeded": 4})
        self.release.set()
        self._wait_until_idle()

    def test_a_fetch_that_finds_the_network_down_ends_with_that_error(self):
        self.offline = True
        self.release.set()
        self.assertEqual(self.client.post("/api/icons").status_code, 202)
        self.assertEqual(self.next_event(self.pending, "icons_done").data,
                         {"fetched": 1, "error": "network unavailable"})
        self._wait_until_idle()

    def test_a_fetch_from_the_command_line_refuses_a_fetch_here(self):
        with logos.icon_job():
            self.assertTrue(self._icons_status()["running"])
            self.assertTrue(self.client.get("/api/run/status").json()["icons"])
            icons = self.client.post("/api/icons")
        self.assertEqual((icons.status_code, icons.json()),
                         (409, {"error": "a company icon fetch is already running"}))
        self.assertFalse(self._icons_status()["running"])

    def test_a_run_holding_the_lock_does_not_refuse_a_fetch(self):
        with pipeline.run_lock():
            self.assertEqual(self.client.post("/api/icons").status_code, 202)
        self.release.set()
        self.assertEqual(self.next_event(self.pending, "icons_done").data,
                         {"fetched": 1, "error": None})
        self._wait_until_idle()

    def test_a_failed_fetch_ends_with_its_error(self):
        def broken(progress=None, limit=None):
            raise RuntimeError("no network")
        self.patch(logos, "fetch_all", broken)
        self.assertEqual(self.client.post("/api/icons").status_code, 202)
        self.assertEqual(self.next_event(self.pending, "icons_done").data,
                         {"fetched": None, "error": "RuntimeError: no network"})
        self._wait_until_idle()

    def test_refused_when_company_icons_are_off_while_stored_icons_are_still_served(self):
        path = logos.path_for("alpha.example", "png")
        path.parent.mkdir(parents=True)
        path.write_bytes(b"\x89PNG\r\n\x1a\n")
        store.save_logo("alpha.example", path, source="site")
        settings.use(dataclasses.replace(settings.get(), company_icons=False))
        response = self.client.post("/api/icons")
        self.assertEqual((response.status_code, response.json()),
                         (409, {"error": "company icons are off in Settings"}))
        self.assertEqual(self.client.get("/api/logos/alpha.example").status_code, 200)

    def test_status_counts_the_companies_of_active_postings(self):
        self.assertEqual(self._icons_status(), {"companies": 5, "resolved": 0,
                                                "with_icon": 0, "pending": 5, "failed": 0,
                                                "running": False})
        store.save_company("alpha", "alpha.example", "clearbit")
        store.save_company("beta", "beta.example", "wikidata")
        store.save_company("delta", "delta.example", "feed")
        store.save_logo("alpha.example", self.home / "logos" / "alpha.example.png",
                        source="site")
        store.save_logo("delta.example", self.home / "logos" / "delta.example.png",
                        source="site")
        self.assertEqual(self._icons_status(), {"companies": 5, "resolved": 2,
                                                "with_icon": 1, "pending": 3, "failed": 0,
                                                "running": False})


class WantedIconsTest(ServerTestCase):
    """The page asks for the icons of the companies it shows with none."""

    def setUp(self):
        super().setUp()
        self.pending, unsubscribe = events.queue_subscriber()
        self.addCleanup(unsubscribe)
        self.fetched = []

        def fetch_wanted(wanted):
            self.fetched.append(wanted)
            return {name: f"{key}.example" for key, names in wanted.items() if key != "beta"
                    for name in names}
        self.patch(logos, "fetch_wanted", fetch_wanted)
        self.patch(status_routes, "WANTED_WAIT", 0.01)

    def _want(self, companies):
        return self.client.post("/api/icons/wanted", json={"companies": companies})

    def test_shown_companies_are_fetched_and_their_icons_reported(self):
        response = self._want(["Alpha", "Beta", "alpha"])
        self.assertEqual((response.status_code, response.json()), (202, {"queued": 2}))
        self.assertEqual(self.next_event(self.pending, "icons_found").data["icons"],
                         {"Alpha": "alpha.example", "alpha": "alpha.example"})
        self.assertEqual(self.fetched, [{"alpha": {"Alpha", "alpha"}, "beta": {"Beta"}}])
        self.assertEqual(self._want(["Alpha"]).json(), {"queued": 0})

    def test_a_batch_waits_for_an_icon_fetch_that_holds_the_marker(self):
        with logos.icon_job():
            self.assertEqual(self._want(["Alpha"]).json(), {"queued": 1})
            time.sleep(0.1)
            self.assertEqual(self.fetched, [])
        self.next_event(self.pending, "icons_found")
        self.assertEqual(self.fetched, [{"alpha": {"Alpha"}}])

    def test_refused_when_company_icons_are_off_or_the_list_is_too_long(self):
        settings.use(dataclasses.replace(settings.get(), company_icons=False))
        self.assertEqual(self._want(["Alpha"]).status_code, 409)
        settings.use(dataclasses.replace(settings.get(), company_icons=True))
        self.assertEqual(self._want(["Alpha"] * 201).status_code, 400)
        self.assertEqual(self._want(["A" * 201]).status_code, 400)
        self.assertEqual(self.fetched, [])


class OnboardTest(ServerTestCase):
    def setUp(self):
        super().setUp()
        self.prompts = []

        def fake_run(prompt, model=None, timeout=240):
            self.prompts.append(prompt)
            return envelope(), None

        self.patch(claude, "run", fake_run)

    def test_a_fresh_home_needs_setup(self):
        self.assertEqual(self.client.get("/api/onboard/status").json(),
                         {"initialised": False, "needs_setup": True})
        cli.init.cmd_init(cli.parser.build_parser().parse_args(["init"]))
        self.assertEqual(self.client.get("/api/onboard/status").json(),
                         {"initialised": True, "needs_setup": True})
        paths.profile_md().write_text("mine", encoding="utf-8")
        self.assertFalse(self.client.get("/api/onboard/status").json()["needs_setup"])

    def test_options_list_the_roles_and_authorizations(self):
        body = self.client.get("/api/onboard/options").json()
        self.assertEqual(body, {"roles": list(onboard.ROLE_KEYWORDS),
                                "work_authorization": list(onboard.WORK_AUTHORIZATION)})

    def test_draft_from_pasted_text(self):
        response = self.client.post("/api/onboard/draft", json={"text": "Sam Lee\nInitech"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), {"profile_md": PROFILE, "suggestions": SUGGESTIONS})
        self.assertIn("<<<RESUME\nSam Lee\nInitech\nRESUME>>>", self.prompts[0])

    def test_a_draft_says_how_much_of_the_resume_it_read(self):
        heard = []
        self.addCleanup(events.subscribe(heard.append))
        self.client.post("/api/onboard/draft", json={"text": "Sam Lee\nInitech"})
        self.assertIn("[setup] read 3 words from the resume",
                      [event.data.get("text") for event in heard if event.kind == "info"])

    def test_pasted_text_that_names_a_file_is_not_read(self):
        secret = self.home / "notes.txt"
        secret.write_text("private", encoding="utf-8")
        self.client.post("/api/onboard/draft", json={"text": str(secret)})
        self.assertNotIn("private", self.prompts[0])

    def test_draft_from_an_uploaded_pdf_or_txt(self):
        pdf_bytes = b"%PDF-1.4\r\n\x00\xff binary\r\n--not-a-boundary\r\n"
        received = []

        def fake_pdftotext(cmd, **kwargs):
            received.append(Path(cmd[2]).read_bytes())
            return subprocess.CompletedProcess(cmd, 0, stdout="Sam Lee from PDF", stderr="")

        self.patch(onboard.resume.subprocess, "run", fake_pdftotext)
        response = self.client.post("/api/onboard/draft",
                                    files={"resume": ("cv.pdf", pdf_bytes, "application/pdf")})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(received, [pdf_bytes])
        self.assertIn("Sam Lee from PDF", self.prompts[-1])

        response = self.client.post("/api/onboard/draft",
                                    files={"resume": ("cv.txt", b"Sam from txt", "text/plain")})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn("Sam from txt", self.prompts[-1])

    def test_draft_is_refused_up_front_when_files_hold_own_content(self):
        paths.profile_md().write_text("mine", encoding="utf-8")
        response = self.client.post("/api/onboard/draft", json={"text": "Sam"})
        self.assertEqual(response.status_code, 409)
        self.assertIn(str(paths.profile_md()), response.json()["error"])
        self.assertEqual(response.json()["files"], [str(paths.profile_md())])
        self.assertEqual(self.prompts, [])

        response = self.client.post("/api/onboard/draft",
                                    json={"text": "Sam", "overwrite": True})
        self.assertEqual(response.status_code, 200, response.text)
        response = self.client.post("/api/onboard/draft", data={"overwrite": "true"},
                                    files={"resume": ("cv.txt", b"Sam", "text/plain")})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(len(self.prompts), 2)
        response = self.client.post("/api/onboard/draft",
                                    json={"text": "Sam", "overwrite": "yes"})
        self.assertEqual(response.status_code, 400)

    def test_bad_input_is_400(self):
        cases = [({"json": {"text": "  "}}, "no text Cairn can read"),
                 ({"json": {"words": "x"}}, "expected"),
                 ({"json": {"text": 3}}, "string"),
                 ({"files": {"resume": ("cv.docx", b"PK", "application/zip")}}, "isn't a PDF"),
                 ({"files": {"other": ("cv.txt", b"x", "text/plain")}}, "'resume'")]
        for kwargs, message in cases:
            with self.subTest(kwargs=kwargs):
                response = self.client.post("/api/onboard/draft", **kwargs)
                self.assertEqual(response.status_code, 400)
                self.assertIn(message, response.json()["error"])
        self.assertEqual(self.prompts, [])

    def test_an_oversized_body_is_413_without_reading_it(self):
        self.patch(onboard_routes, "MAX_RESUME_BYTES", 64)
        response = self.client.post("/api/onboard/draft", content=b'{"text": "Sam"}',
                                    headers={"content-type": "application/json",
                                             "content-length": str(10**9)})
        self.assertEqual(response.status_code, 413)
        self.assertIn("over", response.json()["error"])
        streamed = self.client.post("/api/onboard/draft",
                                    content=iter([b'{"text": "', b"x" * 100, b'"}']),
                                    headers={"content-type": "application/json"})
        self.assertEqual(streamed.status_code, 413)
        self.assertEqual(self.prompts, [])

    def post_raw_upload(self, filename, content_type="multipart/form-data"):
        # built by hand: httpx percent-encodes a NUL in a file name before sending it
        body = (b'--B\r\nContent-Disposition: form-data; name="resume"; filename="'
                + filename + b'"\r\nContent-Type: text/plain\r\n\r\nSam from raw\r\n--B--\r\n')
        return self.client.post("/api/onboard/draft", content=body,
                                headers={"content-type": f"{content_type}; boundary=B"})

    def test_upper_case_multipart_and_bad_file_names(self):
        response = self.post_raw_upload(b"cv.txt", "Multipart/Form-Data")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn("Sam from raw", self.prompts[-1])
        for name in (b"../cv.txt", b"dir\\cv.txt", b"cv\x00.txt"):
            with self.subTest(name=name):
                response = self.post_raw_upload(name)
                self.assertEqual(response.status_code, 400)
                self.assertIn("file name", response.json()["error"])

    def test_apply_is_refused_while_a_run_holds_the_lock(self):
        body = {"profile_md": PROFILE, "prefs": {}}
        with pipeline.run_lock():
            response = self.client.post("/api/onboard/apply", json=body)
        self.assertEqual(response.status_code, 409)
        self.assertIn("already in progress", response.json()["error"])
        self.assertFalse(paths.profile_md().exists())

    def test_a_claude_failure_is_502(self):
        self.patch(claude, "run", lambda *a, **k: (None, "claude exited 1: not logged in"))
        response = self.client.post("/api/onboard/draft", json={"text": "Sam"})
        self.assertEqual(response.status_code, 502)
        self.assertIn("not logged in", response.json()["error"])

    def test_draft_is_refused_while_a_run_holds_the_lock(self):
        with pipeline.run_lock():
            response = self.client.post("/api/onboard/draft", json={"text": "Sam"})
        self.assertEqual(response.status_code, 409)
        self.assertIn("already in progress", response.json()["error"])
        self.assertEqual(self.prompts, [])

    def test_apply_refuses_own_content_until_overwrite(self):
        self.patch(onboard.files.sources, "resolve_company", lambda name: None)
        paths.profile_md().write_text("mine", encoding="utf-8")
        body = {"profile_md": PROFILE,
                "prefs": {"roles": ["ml"], "graduation_year": 2027, "watchlist": ["Nope"]}}
        response = self.client.post("/api/onboard/apply", json=body)
        self.assertEqual(response.status_code, 409)
        self.assertIn(str(paths.profile_md()), response.json()["error"])
        self.assertEqual(response.json()["files"], [str(paths.profile_md())])
        self.assertEqual(paths.profile_md().read_text(encoding="utf-8"), "mine")

        body["prefs"]["overwrite"] = True
        response = self.client.post("/api/onboard/apply", json=body)
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        self.assertEqual(result["written"], [str(paths.profile_md()), str(paths.config_file())])
        self.assertEqual(result["unresolved"], ["Nope"])
        self.assertNotIn("template", result)
        self.assertEqual(result["settings"]["graduation_year"], 2027)
        self.assertEqual(self.client.get("/api/settings").json()["graduation_year"], 2027)
        self.assertEqual(self.client.get("/api/files/profile").json()["text"], PROFILE)

    def test_apply_with_a_bad_answer_is_400(self):
        response = self.client.post("/api/onboard/apply", json={
            "profile_md": PROFILE, "prefs": {"roles": ["astronaut"]}})
        self.assertEqual(response.status_code, 400)
        self.assertIn("astronaut", response.json()["error"])

    def test_seed_holds_the_run_lock(self):
        held = []

        def fake_seed():
            held.append(pipeline.lock_holder())
            return 7

        self.patch(fetch, "seed", fake_seed)
        response = self.client.post("/api/seed")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"seeded": 7})
        self.assertEqual(held, [os.getpid()])
        with pipeline.run_lock():
            self.assertEqual(self.client.post("/api/seed").status_code, 409)
        self.assertEqual(len(held), 1)


class GuardTest(ServerTestCase):
    """Only the app's own page, opened through the launch URL on this Mac, may use the
    API: other machines, hosts, origins, and requests without the session are refused."""

    def bare_client(self, base_url=LOCAL, client=("127.0.0.1", 50000)):
        return self.enterContext(TestClient(create_app(), base_url=base_url, client=client))

    def launched_client(self):
        """A client that opened the launch URL, and so holds the cookie, but sends no
        x-cairn header."""
        client = self.bare_client()
        client.get(f"/?t={security.SESSION}")
        return client

    def test_the_launch_url_trades_its_token_for_a_strict_http_only_cookie(self):
        for path in ("/", "/index.html"):
            with self.subTest(path):
                response = self.bare_client().get(f"{path}?t={security.SESSION}",
                                                  follow_redirects=False)
                self.assertEqual((response.status_code, response.headers["location"]),
                                 (303, "/"))
                self.assertEqual(response.headers["set-cookie"],
                                 f"cairn_session_80={security.SESSION}; HttpOnly; "
                                 f"SameSite=Strict; Path=/")
        page = self.bare_client().get(f"/?t={security.SESSION}")
        self.assertEqual(page.status_code, 200)
        self.assertIn('src="/app.js"', page.text)

    def test_servers_on_two_ports_keep_their_own_cookie(self):
        # a browser keeps one cookie per host and name, whatever the port
        names = []
        for port in (8001, 8002):
            response = self.bare_client(f"{LOCAL}:{port}").get(
                f"/?t={security.SESSION}", follow_redirects=False)
            names.append(response.headers["set-cookie"].partition("=")[0])
        self.assertEqual(names, ["cairn_session_8001", "cairn_session_8002"])
        client = self.bare_client(f"{LOCAL}:8002")
        client.cookies.set("cairn_session_8001", security.SESSION)
        self.assertEqual(client.get("/api/status").status_code, 403)

    def test_the_page_without_the_token_says_how_to_open_the_app(self):
        client = self.bare_client()
        for path in ("/", "/index.html", "/?t=guessed"):
            with self.subTest(path):
                response = client.get(path)
                self.assertEqual(response.status_code, 403)
                self.assertIn("Open the Cairn app", response.text)
                self.assertNotIn("set-cookie", response.headers)
                self.assertNotIn("app.js", response.text)
        self.assertEqual(client.head("/").status_code, 403)
        self.assertEqual(client.get("/app.js").status_code, 200)

    def test_every_api_request_needs_the_session(self):
        client = self.bare_client()
        refused = (403, {"error": "this page lost its link to Cairn. Close it and open "
                                  "Cairn again"})
        for path in ("/api/status", "/api/files/profile", "/api/jobs/alpha", "/api/keys",
                     "/api/run/events"):
            with self.subTest(path):
                response = client.get(path)
                self.assertEqual((response.status_code, response.json()), refused)
        client.cookies.set("cairn_session_80", "guessed")
        self.assertEqual(client.get("/api/status").status_code, 403)
        client.cookies.clear()
        client.get(f"/?t={security.SESSION}")
        self.assertEqual(client.get("/api/status").status_code, 200)

    def test_a_change_needs_the_app_header(self):
        client = self.launched_client()
        response = client.put("/api/jobs/alpha/application", json={"status": "saved"})
        self.assertEqual((response.status_code, response.json()),
                         (403, {"error": "a change must come from the app's own page"}))
        self.assertIsNone(store.get_posting("alpha")["status"])
        response = client.put("/api/jobs/alpha/application", json={"status": "saved"},
                              headers={"x-cairn": "1"})
        self.assertEqual((response.status_code, response.json()["status"]), (200, "saved"))

    def test_a_page_on_another_local_port_is_refused(self):
        for headers in ({"Origin": "http://127.0.0.1:1234"},
                        {"Origin": "http://localhost:1234"},
                        {"Sec-Fetch-Site": "same-site"}):
            with self.subTest(headers):
                response = self.client.put("/api/jobs/alpha/application", headers=headers,
                                           json={"status": "saved"})
                self.assertEqual(response.status_code, 403)
                response = self.client.get("/api/files/profile", headers=headers)
                self.assertEqual(response.status_code, 403)
        self.assertIsNone(store.get_posting("alpha")["status"])
        response = self.client.put("/api/jobs/alpha/application", json={"status": "saved"},
                                   headers={"Origin": LOCAL, "Sec-Fetch-Site": "same-origin"})
        self.assertEqual(response.status_code, 200)

    def test_a_cross_origin_form_post_to_restore_is_refused(self):
        zipped = io.BytesIO()
        with zipfile.ZipFile(zipped, "w") as bundle:
            bundle.writestr("manifest.json", "{}")
        for headers in ({"Origin": "https://evil.example"},
                        {"Origin": "null"},
                        {"Sec-Fetch-Site": "cross-site"}):
            with self.subTest(headers):
                response = self.client.post("/api/restore", headers=headers,
                                            files={"backup": ("b.zip", zipped.getvalue())})
                self.assertEqual(response.status_code, 403)
                self.assertEqual(response.json(),
                                 {"error": "requests from other websites are refused"})
        response = self.launched_client().post(
            "/api/restore", files={"backup": ("b.zip", zipped.getvalue())})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(list(self.home.parent.glob(f"{self.home.name}.bak-*")), [])

    def test_a_rebound_host_is_refused(self):
        for host in ("evil.example", "evil.example:8831", "127.0.0.1.evil.example",
                     "localhost.evil.example", "[::1]evil", ""):
            with self.subTest(host):
                response = self.client.get("/api/status", headers={"Host": host})
                self.assertEqual(response.status_code, 421)
        response = self.bare_client("http://evil.example").get(f"/?t={security.SESSION}")
        self.assertEqual(response.status_code, 421)
        self.assertNotIn("set-cookie", response.headers)

    def test_every_loopback_host_and_origin_is_served(self):
        for host in ("127.0.0.1", "127.0.0.1:8831", "localhost:8831", "[::1]:8831",
                     "LOCALHOST"):
            with self.subTest(host):
                response = self.client.get("/api/status", headers={
                    "Host": host, "Origin": f"http://{host}", "Sec-Fetch-Site": "same-origin"})
                self.assertEqual(response.status_code, 200)
        response = self.client.get("/api/status", headers={"Origin": "https://127.0.0.1"})
        self.assertEqual(response.status_code, 403)

    def test_another_machine_is_refused_whatever_host_it_names(self):
        for address in ("192.168.1.46", "10.0.0.2", "fe80::1", "testclient"):
            with self.subTest(address):
                client = self.bare_client(client=(address, 50000))
                client.cookies.set("cairn_session_80", security.SESSION)
                for path in ("/api/status", f"/?t={security.SESSION}", "/app.js"):
                    response = client.get(path, headers={"x-cairn": "1"})
                    self.assertEqual((response.status_code, response.json()), (403, {
                        "error": "this server answers only requests from this computer"}))
        for address in ("::1", "::ffff:127.0.0.1", "127.0.0.2"):
            with self.subTest(address):
                client = self.bare_client(client=(address, 50000))
                client.get(f"/?t={security.SESSION}")
                self.assertEqual(client.get("/api/status").status_code, 200)

    def test_every_response_carries_the_security_headers(self):
        for path in ("/", "/app.js", "/api/status", "/api/nothing-here"):
            with self.subTest(path):
                headers = self.client.get(path).headers
                self.assertEqual(headers["content-security-policy"],
                                 "default-src 'self'; object-src "
                                 "'none'; base-uri 'none'; form-action 'self'; "
                                 "frame-ancestors 'none'")
                self.assertEqual((headers["x-frame-options"], headers["x-content-type-options"],
                                  headers["referrer-policy"]), ("DENY", "nosniff", "no-referrer"))
        self.assertEqual(self.client.get("/api/status").headers["cache-control"], "no-store")
        self.assertEqual(self.client.get("/app.js").headers["cache-control"], "no-cache")
        refused = self.bare_client().get("/api/status").headers
        self.assertEqual((refused["x-frame-options"], refused["cache-control"]),
                         ("DENY", "no-store"))

    def test_the_favicon_may_style_itself_and_nothing_more(self):
        # its colours, light and dark, sit in an inline <style>
        response = self.client.get("/favicon.svg")
        self.assertEqual((response.status_code, response.headers["content-security-policy"]),
                         (200, "default-src 'none'; style-src 'unsafe-inline'"))
        self.assertEqual(response.headers["x-content-type-options"], "nosniff")

    def test_api_requests_answer_503_while_a_restore_runs(self):
        self.patch(system.maintenance, "restoring", True)
        response = self.client.get("/api/status")
        self.assertEqual((response.status_code, response.json()),
                         (503, {"error": "Cairn is restoring a backup. Try again in a moment"}))
        self.assertEqual(self.client.get("/").status_code, 200)

    def test_restore_through_the_app(self):
        path = backup.export(self.home / "out")
        store.upsert_postings([_row("late", company="Late")], "feed-one")
        response = self.client.post("/api/restore",
                                    files={"backup": ("b.zip", path.read_bytes())})
        self.assertEqual(response.status_code, 202, response.text)
        self.assertFalse(system.maintenance.restoring)
        self.assertIsNone(store.get_posting("late"))
        self.assertEqual(self.client.get("/api/jobs/alpha").status_code, 200)

    def test_a_restore_that_changes_the_provider_waits_for_confirmation(self):
        paths.config_file().write_text('claude_bin = "/tmp/anything"\n'
                                       'llm_base_url = "https://attacker.example/v1"\n',
                                       encoding="utf-8")
        path = backup.export(self.home / "out")
        paths.config_file().unlink()
        upload = {"backup": ("b.zip", path.read_bytes())}
        response = self.client.post("/api/restore", files=upload)
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(response.json()["model_changes"], [
            {"setting": "llm_base_url", "current": "", "incoming": "https://attacker.example/v1"},
            {"setting": "claude_bin", "current": "claude", "incoming": "/tmp/anything"}])
        self.assertFalse(paths.config_file().exists())
        self.assertEqual(list(self.home.parent.glob(f"{self.home.name}.bak-*")), [])
        response = self.client.post("/api/restore?accept_model_change=true", files=upload)
        self.assertEqual(response.status_code, 202, response.text)
        self.assertEqual(settings.base().claude_bin, "/tmp/anything")

    def test_serve_refuses_a_host_other_machines_reach(self):
        with contextlib.redirect_stderr(io.StringIO()) as err, \
                self.assertRaises(SystemExit):
            cli.parser.build_parser().parse_args(["serve", "--host", "0.0.0.0"])
        self.assertIn("invalid choice: '0.0.0.0'", err.getvalue())
        for host in cli.commands.LOCAL_HOSTS:
            with self.subTest(host):
                self.assertEqual(cli.parser.build_parser().parse_args(["serve", "--host", host]).host, host)


class KeysTest(ServerTestCase):
    KEY = "sk-usajobs-0123456789"

    def setUp(self):
        super().setUp()
        self.enterContext(mock.patch.dict(os.environ))
        for name in [n for n in os.environ if n.endswith("_KEY")]:
            del os.environ[name]

    def test_lists_sets_and_deletes_keys_without_returning_one(self):
        names = ["anthropic", "gemini", "groq", "mistral", "openrouter", "usajobs"]
        self.assertEqual(self.client.get("/api/keys").json(), dict.fromkeys(names, False))
        response = self.client.put("/api/keys/usajobs", json={"key": self.KEY})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {**dict.fromkeys(names, False), "usajobs": True})
        self.assertNotIn(self.KEY, response.text)
        self.assertEqual(secrets.get_key("usajobs"), self.KEY)
        response = self.client.delete("/api/keys/usajobs")
        self.assertEqual(response.json()["usajobs"], False)
        self.assertIsNone(secrets.get_key("usajobs"))

    def test_refuses_an_unknown_name_or_a_bad_key(self):
        for name, body, message in [
                ("gpt", {"key": self.KEY}, "no key named 'gpt'"),
                ("claude-code", {"key": self.KEY}, "no key named 'claude-code'"),
                ("usajobs", {"key": "two words"}, "key: the key should be printable ASCII"),
                ("usajobs", {"key": 12}, "key: should be a string"),
                ("usajobs", {"key": self.KEY, "email": "x"}, "unknown field(s): email")]:
            with self.subTest(name=name, body=body):
                response = self.client.put(f"/api/keys/{name}", json=body)
                self.assertEqual(response.status_code, 400)
                self.assertIn(message, response.json()["error"])
        self.assertEqual(self.client.delete("/api/keys/gpt").status_code, 400)
        self.assertFalse(secrets.secrets_file().exists())

    def test_no_settings_export_or_backup_carries_a_key(self):
        self.client.put("/api/settings", json={"fit_threshold": 70})
        self.client.put("/api/keys/usajobs", json={"key": self.KEY})
        self.client.put("/api/keys/groq", json={"key": self.KEY + "-groq"})
        for url in ("/api/settings", "/api/settings/export", "/api/llm", "/api/keys",
                    "/api/llm/providers"):
            with self.subTest(url):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 200)
                self.assertNotIn(self.KEY, response.text)
        with user_home(self.home / "user"):
            path = Path(self.client.post("/api/backup").json()["path"])
        self.assertTrue(path.is_relative_to(self.home))
        with zipfile.ZipFile(path) as bundle:
            self.assertIn("config.toml", bundle.namelist())
            self.assertNotIn("secrets.toml", bundle.namelist())
            for name in bundle.namelist():
                self.assertNotIn(self.KEY.encode(), bundle.read(name), name)


class DroppedEventsTest(unittest.TestCase):
    def test_a_full_queue_becomes_one_warning_frame(self):
        pending = events.DroppingQueue(1)
        pending.offer(events.Event("info", 1.0, {"text": "kept"}))
        pending.offer(events.Event("info", 2.0, {"text": "lost"}))
        self.addCleanup(setattr, events, "queue_subscriber", events.queue_subscriber)
        events.queue_subscriber = lambda: (pending, lambda: None)
        checks = iter([False, False, True])

        async def is_disconnected():
            return next(checks)

        request = SimpleNamespace(
            app=SimpleNamespace(state=SimpleNamespace(shutting_down=lambda: False)),
            is_disconnected=is_disconnected)

        async def frames():
            return [frame async for frame in run_routes._event_stream(request)]

        got = asyncio.run(frames())
        self.assertEqual(got[0], ": keepalive\n\n")
        self.assertTrue(got[1].startswith("event: warn\n"))
        self.assertIn("events dropped: client too slow", got[1])
        self.assertTrue(got[2].startswith("event: info\n"))
        self.assertIn('"text": "kept"', got[2])
        self.assertEqual(len(got), 3)


class EventStreamTest(unittest.TestCase):
    """The stream never ends, which TestClient cannot read, so a real server runs."""

    def test_streams_emitted_events(self):
        # entered before the server starts, so the server stops before the home goes
        self.enterContext(temp_home())
        self.baseline = len(events._subscribers)
        port = server.pick_port()
        srv, thread = server.start_in_thread("127.0.0.1", port)
        self.addCleanup(thread.join, 10)
        self.addCleanup(setattr, srv, "should_exit", True)
        url = f"http://127.0.0.1:{port}/api/run/events"
        self.assertEqual(httpx.get(url, timeout=5).status_code, 403)
        with httpx.stream("GET", url, headers=security.own_headers(url),
                          timeout=5) as response:
            self.assertEqual(response.headers["content-type"],
                             "text/event-stream; charset=utf-8")
            lines = response.iter_lines()
            self.assertEqual(next(lines), ": keepalive")
            self.assertEqual(next(lines), "")
            events.emit("info", text="hello")
            received = [next(lines) for _ in range(3)]
        self.assertEqual(received[0], "event: info")
        self.assertTrue(received[1].startswith("data: {"))
        self.assertIn('"text": "hello"', received[1])
        self.assertEqual(received[2], "")
        self.assertTrue(self.unsubscribed_within(5))

    def unsubscribed_within(self, seconds):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if self.baseline == len(events._subscribers):
                return True
            time.sleep(0.05)
        return False


if __name__ == "__main__":
    unittest.main()


class RetiredKeyTest(ServerTestCase):
    def test_the_usajobs_key_never_rides_in_settings(self):
        secrets.set_key("usajobs", "usajobs-secret-value")
        body = self.client.get("/api/settings").json()
        self.assertNotIn("usajobs_key", body["values"] if "values" in body else body)
        self.assertNotIn("usajobs-secret-value", self.client.get("/api/settings").text)
        self.assertNotIn("usajobs-secret-value", self.client.get("/api/settings/export").text)
        self.client.get("/")
        refused = self.client.put("/api/settings", json={"usajobs_key": "x"})
        self.assertEqual(refused.status_code, 400)
