"""Every source kind maps its API's reply to the one posting shape the store takes.

Fixtures in tests/fixtures are hand-written samples in each API's real shape, or
trimmed live replies with the company renamed.
"""
import dataclasses
import datetime
import json
import threading
import time
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

from helpers import temp_home

from cairn import events, fetch, logos, net, paths, secrets, settings, sources, store

FIXTURES = Path(__file__).parent / "fixtures"
SIMPLIFY = ("https://raw.githubusercontent.com/SimplifyJobs/New-Grad-Positions/dev/"
            ".github/scripts/listings.json")
VANSHB03 = ("https://raw.githubusercontent.com/vanshb03/Summer2027-Internships/dev/"
            ".github/scripts/listings.json")


def _fixture(name):
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def _served(by_fragment):
    """A stand-in for sources._get_json answering with the fixture whose key is in the URL."""
    def get_json(url, timeout=None, limit=None):
        for fragment, reply in by_fragment.items():
            if fragment in url:
                if isinstance(reply, Exception):
                    raise reply
                return _fixture(reply) if isinstance(reply, str) else reply
        raise AssertionError(f"unexpected request for {url}")
    return mock.patch.object(sources, "_get_json", get_json)


def _web_served(by_fragment):
    """A stand-in for sources._web_json, as _served is for _get_json; records each URL
    and the headers sent with it."""
    asked = []

    def web_json(url, deadline, headers=None):
        asked.append((url, headers))
        for fragment, reply in by_fragment.items():
            if fragment in url:
                if isinstance(reply, Exception):
                    raise reply
                return _fixture(reply) if isinstance(reply, str) else reply
        raise AssertionError(f"unexpected request for {url}")
    patch = mock.patch.object(sources, "_web_json", web_json)
    patch.asked = asked
    return patch


def _posted(by_fragment):
    """A stand-in for sources._post_json, as _served is for _get_json; records bodies."""
    bodies = []

    def post_json(url, body, timeout=None):
        bodies.append((url, body))
        for fragment, reply in by_fragment.items():
            if fragment in url:
                return reply(body) if callable(reply) else _fixture(reply)
        raise AssertionError(f"unexpected request for {url}")
    patch = mock.patch.object(sources, "_post_json", post_json)
    patch.bodies = bodies
    return patch


def _no_icons():
    """fetch_new without the icon lookup, which reads company websites."""
    return mock.patch.object(logos, "fetch_missing")


def _unix(iso):
    return datetime.datetime.fromisoformat(iso).timestamp()


NOW = 1790000000.0
DAY = 86400


class GreenhouseTest(unittest.TestCase):
    def setUp(self):
        self.enterContext(_served({"boards-api.greenhouse.io/v1/boards/stripe/": "greenhouse"}))
        self.rows = sources.greenhouse("stripe", "Stripe")

    def test_rows_carry_stable_ids_and_the_posting_shape(self):
        first = self.rows[0]
        self.assertEqual([r["id"] for r in self.rows],
                         ["greenhouse:stripe:7412001", "greenhouse:stripe:7412002",
                          "greenhouse:stripe:7412003"])
        self.assertEqual(first["company_name"], "Stripe")
        self.assertEqual(first["url"], "https://stripe.com/jobs/search?gh_jid=7412001")
        self.assertEqual(first["locations"], ["San Francisco, CA"])
        self.assertIsNone(first["category"])
        self.assertEqual((first["terms"], first["degrees"]), ([], []))
        self.assertTrue(first["active"] and first["is_visible"])

    def test_dates_are_unix_seconds(self):
        first, _, last = self.rows
        self.assertEqual(first["date_posted"], _unix("2026-09-03T13:30:34-04:00"))
        self.assertEqual(first["date_updated"], _unix("2026-09-12T13:25:25-04:00"))
        self.assertEqual(last["date_posted"], _unix("2026-09-15T00:00:00+00:00"))

    def test_without_a_category_relevance_falls_back_to_the_title(self):
        new_grad, senior, _ = self.rows
        self.assertEqual(new_grad["title"], "Software Engineer, New Grad")
        self.assertTrue(fetch._relevant(new_grad))
        self.assertFalse(fetch._relevant(senior))

    def test_a_date_without_a_timezone_is_utc(self):
        job = {**_fixture("greenhouse")["jobs"][0], "first_published": "2026-09-15T00:00:00"}
        with _served({"greenhouse": {"jobs": [job]}}):
            (row,) = sources.greenhouse("stripe", "Stripe")
        self.assertEqual(row["date_posted"], _unix("2026-09-15T00:00:00+00:00"))

    def test_a_job_without_a_title_is_skipped_and_the_rest_kept(self):
        jobs = _fixture("greenhouse")["jobs"]
        del jobs[1]["title"]
        with _served({"greenhouse": {"jobs": jobs}}):
            rows = sources.greenhouse("stripe", "Stripe")
        self.assertEqual([r["id"] for r in rows],
                         ["greenhouse:stripe:7412001", "greenhouse:stripe:7412003"])

    def test_jobs_on_one_careers_page_stay_distinct(self):
        """Stripe's apply URLs differ only in gh_jid."""
        self.assertEqual(len({store.url_key(r) for r in self.rows}), 3)


class LeverTest(unittest.TestCase):
    def setUp(self):
        self.enterContext(_served({"api.lever.co/v0/postings/figma": "lever"}))
        self.rows = sources.lever("figma", "Figma")

    def test_rows_carry_stable_ids_and_every_location(self):
        first = self.rows[0]
        self.assertEqual(first["id"], "lever:figma:6ed76ce8-4156-4b60-b120-403538bd66cd")
        self.assertEqual(first["title"], "Software Engineer, Early Career")
        self.assertEqual(first["url"],
                         "https://jobs.lever.co/figma/6ed76ce8-4156-4b60-b120-403538bd66cd")
        self.assertEqual(first["locations"], ["New York, NY", "San Francisco, CA"])

    def test_created_at_milliseconds_become_seconds(self):
        self.assertEqual(self.rows[0]["date_posted"], 1786469891.368)
        self.assertEqual(self.rows[0]["date_updated"], 1786469891.368)

    def test_a_remote_workplace_is_named_in_the_locations(self):
        self.assertEqual(self.rows[1]["locations"], ["United States", "Remote"])

    def test_a_posting_with_no_location_has_an_empty_list(self):
        self.assertEqual(self.rows[2]["locations"], [])

    def test_a_posting_without_a_url_is_skipped_and_the_rest_kept(self):
        jobs = _fixture("lever")
        del jobs[0]["hostedUrl"]
        with _served({"lever": jobs}):
            rows = sources.lever("figma", "Figma")
        self.assertEqual([r["title"] for r in rows], ["Platform Engineer", "Product Designer"])


class AshbyTest(unittest.TestCase):
    def setUp(self):
        self.enterContext(_served({"api.ashbyhq.com/posting-api/job-board/openai": "ashby"}))
        self.rows = sources.ashby("openai", "OpenAI")

    def test_unlisted_jobs_are_skipped(self):
        self.assertEqual([r["title"] for r in self.rows],
                         ["Software Engineer, Applied AI", "Research Engineer"])

    def test_a_job_without_an_id_is_skipped_and_the_rest_kept(self):
        data = _fixture("ashby")
        del data["jobs"][0]["id"]
        with _served({"ashby": data}):
            rows = sources.ashby("openai", "OpenAI")
        self.assertEqual([r["title"] for r in rows], ["Research Engineer"])

    def test_rows_carry_stable_ids_locations_and_dates(self):
        first = self.rows[0]
        self.assertEqual(first["id"], "ashby:openai:8fb1615c-34bf-47c4-a1d1-b7b2f836bbd3")
        self.assertEqual(first["company_name"], "OpenAI")
        self.assertEqual(first["locations"], ["San Francisco", "New York City"])
        self.assertEqual(first["date_posted"], _unix("2026-03-12T16:38:15.322+00:00"))


class WorkdayTest(unittest.TestCase):
    LOCATION = "acme.wd5/AcmeCareers"

    def setUp(self):
        self.enterContext(mock.patch("time.time", return_value=NOW))
        self.post = _posted({"acme.wd5.myworkdayjobs.com": "workday"})
        with self.post:
            self.rows = sources.workday(self.LOCATION, "Acme")

    def test_the_board_api_is_asked_for_the_first_page(self):
        url, body = self.post.bodies[0]
        self.assertEqual(url, "https://acme.wd5.myworkdayjobs.com/wday/cxs/acme/AcmeCareers/jobs")
        self.assertEqual(body, {"appliedFacets": {}, "limit": 20, "offset": 0,
                                "searchText": ""})

    def test_rows_carry_stable_ids_and_urls_on_the_board_host(self):
        first = self.rows[0]
        self.assertEqual(first["id"], sources._path_safe(
            "workday:acme.wd5/AcmeCareers:Senior-Software-Engineer--Widget-Runtime_R0000101"))
        self.assertEqual(first["url"], "https://acme.wd5.myworkdayjobs.com/AcmeCareers/job/"
                                       "US-OR-Springfield/Senior-Software-Engineer--Widget-"
                                       "Runtime_R0000101")
        self.assertEqual((first["company_name"], first["category"], first["terms"]),
                         ("Acme", None, []))

    def test_posted_on_text_becomes_unix_seconds(self):
        self.assertEqual([r["date_posted"] for r in self.rows],
                         [NOW, NOW - DAY, NOW - 2 * DAY, NOW - 31 * DAY])
        self.assertEqual(sources._workday_posted("Posted 3 Days Ago", NOW), NOW - 3 * DAY)
        self.assertIsNone(sources._workday_posted("Posted recently", NOW))
        self.assertTrue(all(r["date_is_relative"] for r in self.rows))

    def test_a_stored_posting_keeps_the_date_it_was_first_given(self):
        self.enterContext(temp_home())
        store.upsert_postings(self.rows, "workday:acme.wd5/AcmeCareers")
        with mock.patch("time.time", return_value=NOW + 5 * DAY), self.post:
            later = sources.workday(self.LOCATION, "Acme")
        store.upsert_postings(later, "workday:acme.wd5/AcmeCareers")
        self.assertEqual([store.get_posting(r["id"])["date_posted"] for r in self.rows],
                         [NOW, NOW - DAY, NOW - 2 * DAY, NOW - 31 * DAY])

    def test_a_job_on_two_pages_is_kept_once(self):
        job = _fixture("workday")["jobPostings"][0]

        def page(body):
            # the list shifted by one job between the two requests
            first = max(0, body["offset"] - 1)
            return {"total": 40, "jobPostings": [
                {**job, "externalPath": f"/job/x/J_{first + i}"} for i in range(20)]}
        with _posted({"myworkdayjobs.com": page}):
            rows = sources.workday(self.LOCATION, "Acme")
        ids = [r["id"] for r in rows]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(len(ids), 39)

    def test_a_later_page_that_fails_keeps_the_pages_before_it(self):
        job = _fixture("workday")["jobPostings"][0]
        failure = urllib.error.HTTPError("https://x", 500, "Server Error", {}, None)
        self.addCleanup(failure.close)

        def page(body):
            if body["offset"] == 40:
                raise failure
            return {"total": 80, "jobPostings": [
                {**job, "externalPath": f"/job/x/J_{body['offset'] + i}"} for i in range(20)]}
        heard = []
        self.addCleanup(events.subscribe(heard.append))
        with _posted({"myworkdayjobs.com": page}):
            rows = sources.workday(self.LOCATION, "Acme")
        self.assertEqual(len(rows), 40)
        self.assertEqual([(e.kind, e.data["text"]) for e in heard],
                         [("warn", "[fetch] workday:acme.wd5/AcmeCareers: stopped after 40 of "
                                   "80 jobs: HTTP Error 500: Server Error")])

    def test_a_location_count_is_replaced_by_the_first_place_in_the_path(self):
        self.assertEqual([r["locations"] for r in self.rows],
                         [["US OR Springfield"], ["US, OR, Springfield"],
                          ["US, OR, Springfield"], ["Freedonia, Fredonia"]])

    def test_paging_uses_the_first_total_and_stops_there(self):
        job = _fixture("workday")["jobPostings"][0]

        def page(body):
            jobs = [{**job, "externalPath": f"/job/x/J_{body['offset'] + i}"}
                    for i in range(min(20, 45 - body["offset"]))]
            # Workday reports the total on the first page only
            return {"total": 45 if body["offset"] == 0 else 0, "jobPostings": jobs}
        post = _posted({"myworkdayjobs.com": page})
        with post:
            rows = sources.workday(self.LOCATION, "Acme")
        self.assertEqual(sorted(body["offset"] for _, body in post.bodies), [0, 20, 40])
        self.assertEqual([r["url"].rsplit("_", 1)[1] for r in rows], [str(i) for i in range(45)])
        self.assertEqual(len({r["id"] for r in rows}), 45)

    def test_paging_stops_at_the_cap(self):
        job = _fixture("workday")["jobPostings"][0]

        def page(body):
            return {"total": 5000, "jobPostings": [
                {**job, "externalPath": f"/job/x/J_{body['offset'] + i}"} for i in range(20)]}
        post = _posted({"myworkdayjobs.com": page})
        with post:
            rows = sources.workday(self.LOCATION, "Acme")
        self.assertEqual((len(rows), len(post.bodies)), (sources.WORKDAY_MAX_JOBS, 25))

    def test_pages_not_back_by_the_time_limit_are_left_out_with_a_warning(self):
        job = _fixture("workday")["jobPostings"][0]
        release = threading.Event()
        self.addCleanup(release.set)

        workers = []

        def page(body):
            workers.append(threading.current_thread())
            if body["offset"] > 20:
                release.wait(10)
            return {"total": 100, "jobPostings": [
                {**job, "externalPath": f"/job/x/J_{body['offset'] + i}"} for i in range(20)]}
        heard = []
        self.addCleanup(events.subscribe(heard.append))
        with _posted({"myworkdayjobs.com": page}), \
                mock.patch.object(sources, "FETCH_TIMEOUT", 0.2):
            rows = sources.workday(self.LOCATION, "Acme")
        self.assertEqual(len(rows), 40)
        self.assertEqual([(e.kind, e.data["text"]) for e in heard],
                         [("warn", "[fetch] workday:acme.wd5/AcmeCareers: stopped after 40 of "
                                   "100 jobs at the 0.2 s limit")])
        # a worker left waiting never holds up the interpreter's exit
        self.assertTrue(all(worker.daemon for worker in workers[1:]))

    def test_an_empty_page_ends_paging(self):
        post = _posted({"myworkdayjobs.com": lambda body: {"total": 0, "jobPostings": []}})
        with post:
            self.assertEqual(sources.workday(self.LOCATION, "Acme"), [])
        self.assertEqual(len(post.bodies), 1)

    def test_a_malformed_location_is_an_error(self):
        with self.assertRaises(ValueError):
            sources.workday("acme/AcmeCareers", "Acme")


class SmartRecruitersTest(unittest.TestCase):
    def setUp(self):
        self.enterContext(_served({"api.smartrecruiters.com/v1/companies/Initech/postings":
                                   "smartrecruiters"}))
        self.rows = sources.smartrecruiters("Initech", "Initech")

    def test_rows_carry_stable_ids_urls_and_dates(self):
        first = self.rows[0]
        self.assertEqual(first["id"], "smartrecruiters:Initech:744000151763959")
        self.assertEqual(first["url"], "https://jobs.smartrecruiters.com/Initech/744000151763959")
        self.assertEqual(first["date_posted"], _unix("2026-09-25T05:51:50.168+00:00"))

    def test_non_breaking_spaces_in_titles_become_spaces(self):
        self.assertEqual(self.rows[0]["title"], "Senior Applications Dev Engineer")

    def test_locations_drop_empty_parts_and_name_remote_work(self):
        self.assertEqual(self.rows[0]["locations"], ["Hyderabad, India"])
        self.assertEqual(self.rows[1]["locations"], ["Santa Clara, CALIFORNIA, United States"])
        self.assertIn("Remote", self.rows[3]["locations"])

    def test_paging_stops_at_total_found(self):
        requested = []

        def get_json(url, timeout=None, limit=None):
            requested.append(url)
            offset = int(url.rsplit("offset=", 1)[1])
            return {"totalFound": 250, "content": [
                {"id": str(offset + i), "name": "SWE"} for i in range(min(100, 250 - offset))]}
        with mock.patch.object(sources, "_get_json", get_json):
            rows = sources.smartrecruiters("Initech", "Initech")
        self.assertEqual([url.rsplit("offset=", 1)[1] for url in requested], ["0", "100", "200"])
        self.assertEqual(len(rows), 250)


class WorkableTest(unittest.TestCase):
    def setUp(self):
        self.post = _posted({"apply.workable.com/api/v3/accounts/globex/jobs": "workable"})
        with self.post:
            self.rows = sources.workable("globex", "Globex")

    def test_rows_carry_stable_ids_urls_and_dates(self):
        first = self.rows[0]
        self.assertEqual([r["id"] for r in self.rows],
                         ["workable:globex:2527943", "workable:globex:2878972",
                          "workable:globex:6140066", "workable:globex:6131311"])
        self.assertEqual(first["url"], "https://apply.workable.com/globex/j/97D188CA7C/")
        self.assertEqual(first["date_posted"], _unix("2026-09-24T00:00:00+00:00"))

    def test_locations_name_remote_work(self):
        self.assertEqual(self.rows[0]["locations"], ["Poland", "Remote"])
        self.assertEqual(self.rows[2]["locations"], ["Metamorfosi, Attica, Greece"])

    def test_paging_follows_next_page_tokens(self):
        job = _fixture("workable")["results"][0]
        pages = {None: {"results": [{**job, "id": 1}], "nextPage": "t1"},
                 "t1": {"results": [{**job, "id": 2}], "nextPage": "t2"},
                 "t2": {"results": [{**job, "id": 3}]}}
        post = _posted({"workable": lambda body: pages[body.get("token")]})
        with post:
            rows = sources.workable("globex", "Globex")
        self.assertEqual([r["id"] for r in rows],
                         ["workable:globex:1", "workable:globex:2", "workable:globex:3"])
        self.assertEqual([body.get("token") for _, body in post.bodies], [None, "t1", "t2"])

    def test_paging_stops_at_an_empty_page_whatever_the_token(self):
        job = _fixture("workable")["results"][0]
        pages = {None: {"results": [job], "nextPage": "t1"},
                 "t1": {"results": [], "nextPage": "t2"}}
        post = _posted({"workable": lambda body: pages[body.get("token")]})
        with post:
            self.assertEqual(len(sources.workable("globex", "Globex")), 1)
        self.assertEqual(len(post.bodies), 2)

    def test_paging_stops_after_twenty_pages(self):
        job = _fixture("workable")["results"][0]
        post = _posted({"workable": lambda body: {"results": [job], "nextPage": "again"}})
        with post:
            sources.workable("globex", "Globex")
        self.assertEqual(len(post.bodies), sources.WORKABLE_MAX_PAGES)


class BambooHRTest(unittest.TestCase):
    def setUp(self):
        self.enterContext(_served({"hooli.bamboohr.com/careers/list": "bamboohr",
                                   "hooli.bamboohr.com/careers/": "bamboohr_detail"}))
        self.rows = sources.bamboohr("hooli", "Hooli")

    def test_rows_carry_stable_ids_urls_and_locations(self):
        self.assertEqual([r["id"] for r in self.rows],
                         ["bamboohr:hooli:30", "bamboohr:hooli:42", "bamboohr:hooli:65",
                          "bamboohr:hooli:77"])
        self.assertEqual(self.rows[1]["url"], "https://hooli.bamboohr.com/careers/42")
        self.assertEqual(self.rows[1]["locations"], ["Raleigh, North Carolina"])

    def test_the_posted_date_comes_from_each_jobs_detail(self):
        self.assertEqual(self.rows[1]["date_posted"], _unix("2023-10-06T00:00:00+00:00"))

    def test_a_remote_job_names_remote_in_its_locations(self):
        data = _fixture("bamboohr")
        data["result"][1]["isRemote"] = True
        with _served({"careers/list": data, "careers/": "bamboohr_detail"}):
            rows = sources.bamboohr("hooli", "Hooli")
        self.assertEqual(rows[1]["locations"], ["Raleigh, North Carolina", "Remote"])

    def test_details_not_back_by_the_time_limit_leave_their_jobs_undated(self):
        release = threading.Event()
        self.addCleanup(release.set)

        def get_json(url, timeout=None, limit=None):
            if "careers/list" in url:
                return _fixture("bamboohr")
            if "/42/" not in url:
                release.wait(10)
            return _fixture("bamboohr_detail")
        heard = []
        self.addCleanup(events.subscribe(heard.append))
        with mock.patch.object(sources, "_get_json", get_json), \
                mock.patch.object(sources, "FETCH_TIMEOUT", 0.2):
            rows = sources.bamboohr("hooli", "Hooli")
        self.assertEqual([r["id"] for r in rows if r["date_posted"]], ["bamboohr:hooli:42"])
        self.assertEqual(len(rows), 4)
        self.assertEqual([e.kind for e in heard], ["warn"])
        self.assertIn("left undated at the 0.2 s limit", heard[0].data["text"])

    def test_a_detail_that_fails_leaves_its_job_undated(self):
        def get_json(url, timeout=None, limit=None):
            if "careers/list" in url:
                return _fixture("bamboohr")
            if "/42/" in url:
                return _fixture("bamboohr_detail")
            raise urllib.error.URLError("connection reset")
        heard = []
        self.addCleanup(events.subscribe(heard.append))
        with mock.patch.object(sources, "_get_json", get_json):
            rows = sources.bamboohr("hooli", "Hooli")
        self.assertEqual([r["id"] for r in rows if r["date_posted"]], ["bamboohr:hooli:42"])
        self.assertEqual(len(rows), 4)
        self.assertEqual([(e.kind, e.data["text"]) for e in heard],
                         [("warn", "[fetch] bamboohr:hooli: 3 jobs left undated, their details "
                                   "failed to load: <urlopen error connection reset>")])

    def test_a_slug_that_is_not_a_hostname_label_is_never_requested(self):
        with _served({}), self.assertRaises(ValueError):
            sources.bamboohr("hooli.evil.example/x", "Hooli")


class GithubListingsTest(unittest.TestCase):
    def test_a_season_becomes_the_terms_list(self):
        with _served({VANSHB03: "vanshb03_intern"}):
            rows = sources.github_listings(VANSHB03)
        self.assertEqual([r["terms"] for r in rows], [["Summer 2027"], ["Fall 2026"], []])
        self.assertIsNone(rows[0].get("category"))

    def test_simplify_rows_pass_through(self):
        with _served({SIMPLIFY: "simplify"}):
            self.assertEqual(sources.github_listings(SIMPLIFY), _fixture("simplify"))

    def test_a_body_that_is_not_a_list_is_a_failed_fetch(self):
        heard = []
        self.addCleanup(events.subscribe(heard.append))
        with _served({SIMPLIFY: {}}):
            self.assertEqual(sources.fetch_all([sources.Source("github", SIMPLIFY)]), [])
        self.assertEqual([e.kind for e in heard], ["warn"])


class SourceTest(unittest.TestCase):
    def test_a_github_source_is_named_by_its_repo(self):
        self.assertEqual(sources.Source("github", SIMPLIFY).name,
                         "SimplifyJobs/New-Grad-Positions")

    def test_a_board_is_named_by_kind_and_slug_and_titles_its_slug(self):
        board = sources.Source("lever", "figma")
        self.assertEqual((board.name, board.company), ("lever:figma", "Figma"))
        self.assertEqual(sources.Source("ashby", "openai", "OpenAI").company, "OpenAI")


class FetchAllTest(unittest.TestCase):
    def test_a_failing_source_is_left_out_with_a_warning(self):
        heard = []
        self.addCleanup(events.subscribe(heard.append))
        specs = [sources.Source("greenhouse", "stripe"), sources.Source("lever", "figma"),
                 sources.Source("ashby", "openai")]
        with _served({"greenhouse": "greenhouse", "lever": OSError("timed out"),
                      "ashby": "ashby"}):
            batches = sources.fetch_all(specs)
        self.assertEqual([(name, len(rows)) for name, rows in batches],
                         [("greenhouse:stripe", 3), ("ashby:openai", 2)])
        warnings = [e.data["text"] for e in heard if e.kind == "warn"]
        self.assertEqual(len(warnings), 1)
        self.assertIn("lever:figma", warnings[0])


def _net_served(replies):
    """A stand-in for net.get that records each URL it is asked for and answers by
    matching a fragment of it: bytes are the raw body, anything else is JSON-encoded
    first, and an exception is raised. A URL matching no reply gets a 404.
    """
    requested = []

    def get(url, *, limit, deadline, headers=None, method="GET", data=None):
        requested.append(url)
        for fragment, body in replies.items():
            if fragment in url:
                if isinstance(body, Exception):
                    raise body
                raw = body if isinstance(body, bytes) else json.dumps(body).encode()
                return net.Response(raw, "application/json", url, 200)
        raise net.Failure("HTTP 404", 404)
    patch = mock.patch.object(net, "get", get)
    patch.requested = requested
    return patch


class ResolveCompanyTest(unittest.TestCase):
    def _no_requests(self):
        patch = _net_served({})
        self.addCleanup(lambda: self.assertEqual(patch.requested, []))
        return patch

    def test_board_urls_resolve_without_a_request(self):
        cases = {
            "https://boards.greenhouse.io/stripe": ("greenhouse", "stripe"),
            "https://job-boards.greenhouse.io/x/jobs/123": ("greenhouse", "x"),
            "https://boards.greenhouse.io/embed/job_board?for=stripe": ("greenhouse", "stripe"),
            "https://boards-api.greenhouse.io/v1/boards/stripe/jobs": ("greenhouse", "stripe"),
            "https://jobs.lever.co/figma": ("lever", "figma"),
            "jobs.lever.co/figma": ("lever", "figma"),
            "https://api.lever.co/v0/postings/figma?mode=json": ("lever", "figma"),
            "https://jobs.ashbyhq.com/openai/": ("ashby", "openai"),
            "https://api.ashbyhq.com/posting-api/job-board/openai": ("ashby", "openai"),
            "https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite":
                ("workday", "nvidia.wd5/NVIDIAExternalCareerSite"),
            "https://acme.wd1.myworkdayjobs.com/en-US/Careers": ("workday", "acme.wd1/Careers"),
            "https://acme.wd3.myworkdayjobs.com/External/job/US-Remote/SWE_R1":
                ("workday", "acme.wd3/External"),
            "https://acme.wd103.myworkdayjobs.com/wday/cxs/acme/External/jobs":
                ("workday", "acme.wd103/External"),
            "https://jobs.smartrecruiters.com/Initech": ("smartrecruiters", "Initech"),
            "https://jobs.smartrecruiters.com/Initech/744000151763959":
                ("smartrecruiters", "Initech"),
            "https://careers.smartrecruiters.com/Initech": ("smartrecruiters", "Initech"),
            "https://api.smartrecruiters.com/v1/companies/Initech/postings":
                ("smartrecruiters", "Initech"),
            "https://apply.workable.com/globex/": ("workable", "globex"),
            "https://apply.workable.com/globex/j/97D188CA7C/": ("workable", "globex"),
            "https://hooli.bamboohr.com/careers": ("bamboohr", "hooli"),
            "hooli.bamboohr.com/careers/42": ("bamboohr", "hooli"),
        }
        with self._no_requests():
            for url, (kind, slug) in cases.items():
                with self.subTest(url):
                    self.assertEqual(sources.resolve_company(url),
                                     sources.Source(kind, slug))

    def test_other_urls_on_board_hosts_resolve_to_nothing(self):
        cases = ["https://boards.greenhouse.io/", "https://boards.greenhouse.io/embed/job_board",
                 "https://boards.greenhouse.io/embed/job_app?token=1",
                 "https://boards-api.greenhouse.io/v2/boards/stripe",
                 "https://api.lever.co/v1/postings/figma", "https://jobs.lever.co",
                 "https://api.ashbyhq.com/graphql", "https://careers.example.com/jobs",
                 "https://evil-greenhouse.io/stripe",
                 "https://acme.myworkdayjobs.com/External", "https://acme.wd5.myworkdayjobs.com/",
                 "https://acme.wd5.myworkdayjobs.com/en-US", "https://myworkdayjobs.com/x",
                 "https://acme.wd5.myworkdayjobs.com/job/US/SWE_R1",
                 "https://apply.workable.com/",
                 "https://apply.workable.com/api/v3/accounts/g/jobs",
                 "https://apply.workable.com/j/97D188CA7C",
                 "https://www.bamboohr.com/careers", "https://bamboohr.com/careers",
                 "https://jobs.smartrecruiters.com/"]
        with self._no_requests():
            for url in cases:
                with self.subTest(url):
                    self.assertIsNone(sources.resolve_company(url))

    def test_a_domain_is_not_probed_as_a_name(self):
        with self._no_requests():
            self.assertIsNone(sources.resolve_company("stripe.com"))

    def test_a_board_that_cannot_be_read_is_not_a_match(self):
        failures = {
            "URLError": urllib.error.URLError("no route"),
            "timeout": TimeoutError("timed out"),
            "bad JSON": b"<html>not json</html>",
            "list body": [{"id": 1}],
        }
        for label, reply in failures.items():
            with self.subTest(label), _net_served({"greenhouse": reply,
                                                 "ashby": _fixture("ashby")}):
                self.assertEqual(sources.resolve_company("OpenAI"),
                                 sources.Source("ashby", "openai", "OpenAI"))

    def test_a_name_is_url_quoted_when_probed(self):
        patch = _net_served({})
        with patch:
            self.assertIsNone(sources.resolve_company("AT&T"))
        self.assertIn("https://boards-api.greenhouse.io/v1/boards/at%26t/jobs?content=false",
                      patch.requested)

    def test_a_bare_name_takes_the_first_board_that_lists_jobs(self):
        replies = {"api.lever.co/v0/postings/openai": [],
                   "api.ashbyhq.com/posting-api/job-board/openai": _fixture("ashby")}
        with _net_served(replies):
            self.assertEqual(sources.resolve_company("OpenAI"),
                             sources.Source("ashby", "openai", "OpenAI"))

    def test_a_board_bigger_than_a_feed_reply_is_read(self):
        # openai's Ashby board ran 13.8 MB in September 2026
        board = {**_fixture("ashby"), "padding": " " * (sources.FEED_MAX_BYTES + 1)}
        replies = {"api.lever.co/v0/postings/openai": [],
                   "api.ashbyhq.com/posting-api/job-board/openai": board}
        with _net_served(replies):
            self.assertEqual(sources.resolve_company("OpenAI"),
                             sources.Source("ashby", "openai", "OpenAI"))
            self.assertEqual(len(sources.ashby("openai", "OpenAI")), 2)

    def test_a_multi_word_name_is_tried_as_one_slug(self):
        with _net_served({"boards-api.greenhouse.io/v1/boards/janestreet/": _fixture("greenhouse")}):
            self.assertEqual(sources.resolve_company("Jane Street"),
                             sources.Source("greenhouse", "janestreet", "Jane Street"))

    def test_a_name_no_board_knows_resolves_to_nothing(self):
        patch = _net_served({})
        with patch:
            self.assertIsNone(sources.resolve_company("Nonexistent Co"))
        self.assertEqual([url.split("/")[2] for url in patch.requested],
                         ["boards-api.greenhouse.io", "api.lever.co", "api.ashbyhq.com",
                          "api.smartrecruiters.com", "apply.workable.com",
                          "nonexistentco.bamboohr.com"])

    def test_a_bare_name_can_resolve_to_each_newer_board(self):
        cases = {
            "smartrecruiters": {"api.smartrecruiters.com/v1/companies/initech/postings":
                                _fixture("smartrecruiters")},
            "workable": {"api.smartrecruiters.com": {"totalFound": 0, "content": []},
                         "apply.workable.com/api/v3/accounts/initech/jobs": _fixture("workable")},
            "bamboohr": {"apply.workable.com": {"total": 0, "results": []},
                         "initech.bamboohr.com/careers/list": _fixture("bamboohr")},
        }
        for kind, replies in cases.items():
            with self.subTest(kind), _net_served(replies):
                self.assertEqual(sources.resolve_company("Initech"),
                                 sources.Source(kind, "initech", "Initech"))

    def test_a_workday_board_is_named_by_its_tenant(self):
        board = sources.resolve_company("https://nvidia.wd5.myworkdayjobs.com/en-US/External")
        self.assertEqual((board.name, board.company), ("workday:nvidia.wd5/External", "Nvidia"))


class SettingsTest(unittest.TestCase):
    def setUp(self):
        self.enterContext(temp_home())

    def _load(self, text):
        paths.config_file().write_text(text, encoding="utf-8")
        return settings.load()

    def test_sources_and_watchlist_round_trip_as_arrays_of_tables(self):
        original = dataclasses.replace(
            settings.defaults(),
            fit_threshold=70,
            sources=[{"kind": "github", "location": SIMPLIFY},
                     {"kind": "github", "location": VANSHB03, "enabled": False}],
            watchlist=[{"kind": "greenhouse", "location": "stripe", "company": "Stripe"},
                       {"kind": "ashby", "location": "openai"},
                       {"kind": "workday", "location": "acme.wd5/External", "company": "Acme"},
                       {"kind": "smartrecruiters", "location": "Initech"},
                       {"kind": "workable", "location": "globex"},
                       {"kind": "bamboohr", "location": "hooli"}])
        settings.save(original)
        text = paths.config_file().read_text(encoding="utf-8")
        self.assertIn('[[sources]]\nkind = "github"\nlocation = "https://raw', text)
        self.assertIn('[[watchlist]]\nkind = "ashby"\nlocation = "openai"', text)
        self.assertLess(text.index("fit_threshold"), text.index("[["))
        self.assertEqual(settings.load(), original)

    def test_an_emptied_source_list_round_trips(self):
        original = dataclasses.replace(settings.defaults(), sources=[])
        settings.save(original)
        self.assertEqual(settings.load(), original)

    def test_a_bad_spec_is_rejected_naming_its_index(self):
        cases = {
            '[[sources]]\nkind = "icims"\nlocation = "x"\n': "sources[0]",
            '[[watchlist]]\nkind = "workday"\nlocation = "acme/External"\n': "watchlist[0]",
            '[[watchlist]]\nkind = "lever"\nlocation = "figma"\n'
            '[[watchlist]]\nkind = "lever"\n': "watchlist[1]",
            '[[watchlist]]\nkind = "github"\nlocation = "' + SIMPLIFY + '"\n': "watchlist[0]",
            '[[sources]]\nkind = "github"\nlocation = "https://example.com"\n': "sources[0]",
            '[[sources]]\nkind = "lever"\nlocation = "figma"\nslug = "x"\n': "sources[0]",
            '[[sources]]\nkind = "lever"\nlocation = "figma"\nenabled = "no"\n': "sources[0]",
        }
        for text, where in cases.items():
            with self.subTest(text):
                with self.assertRaises(settings.SettingsError) as caught:
                    self._load(text)
                self.assertIn(where, str(caught.exception))


class FetchFromSourcesTest(unittest.TestCase):
    def setUp(self):
        self.enterContext(temp_home(
            sources=[{"kind": "github", "location": SIMPLIFY}],
            watchlist=[{"kind": "greenhouse", "location": "stripe", "company": "Stripe"}]))
        self.enterContext(_no_icons())

    def test_a_github_row_and_a_board_row_with_one_apply_url_keep_one_posting(self):
        with _served({SIMPLIFY: "simplify", "greenhouse": "greenhouse"}):
            batches = fetch._load_all()
        self.assertEqual([(name, len(rows)) for name, rows in batches],
                         [("SimplifyJobs/New-Grad-Positions", 3), ("greenhouse:stripe", 2)])
        stripe_new_grad = [r for _, rows in batches for r in rows
                           if r["url"] == "https://stripe.com/jobs/search?gh_jid=7412001"]
        self.assertEqual([r["category"] for r in stripe_new_grad], ["Software"])

    def test_a_disabled_source_is_not_fetched(self):
        cfg = settings.get()
        settings.use(dataclasses.replace(cfg, watchlist=[{**cfg.watchlist[0], "enabled": False}]))
        with _served({SIMPLIFY: "simplify"}):
            self.assertEqual([name for name, _ in fetch._load_all()],
                             ["SimplifyJobs/New-Grad-Positions"])

    def test_an_icon_lookup_that_fails_is_a_warning_and_the_run_goes_on(self):
        heard = []
        self.addCleanup(events.subscribe(heard.append))
        with _served({SIMPLIFY: "simplify", "greenhouse": "greenhouse"}), \
                mock.patch.object(logos, "fetch_missing",
                                  side_effect=RuntimeError("database is locked")):
            _, counts = fetch.fetch_new()
        self.assertEqual(counts["total"], 5)
        self.assertIn(("warn", "[logos] skipped: RuntimeError: database is locked"),
                      [(e.kind, e.data["text"]) for e in heard])

    def test_a_dry_run_skips_icon_and_ranked_link_lookups(self):
        """Neither is worth its time when nothing here will be ranked."""
        with _served({SIMPLIFY: "simplify", "greenhouse": "greenhouse"}), \
                mock.patch.object(fetch, "check_links") as check_links:
            fetch.fetch_new(dry_run=True)
        self.assertEqual(logos.fetch_missing.call_count, 0)
        check_links.assert_not_called()

    def test_icons_false_skips_the_icon_lookup_and_still_checks_ranked_links(self):
        with _served({SIMPLIFY: "simplify", "greenhouse": "greenhouse"}), \
                mock.patch.object(fetch, "check_links") as check_links:
            fetch.fetch_new(icons=False)
        self.assertEqual(logos.fetch_missing.call_count, 0)
        check_links.assert_called_once()

    def test_postings_are_stored_under_the_source_name(self):
        with _served({SIMPLIFY: "simplify", "greenhouse": "greenhouse"}):
            fetch.fetch_new()
        rows = store.connect().execute(
            "SELECT source, count(*) FROM postings GROUP BY source ORDER BY source").fetchall()
        self.assertEqual([tuple(r) for r in rows],
                         [("SimplifyJobs/New-Grad-Positions", 3), ("greenhouse:stripe", 2)])


class LegacyTest(unittest.TestCase):
    """Legacy rows predate named sources and came from the GitHub feeds."""

    def setUp(self):
        self.enterContext(temp_home(
            sources=[{"kind": "github", "location": SIMPLIFY}],
            watchlist=[{"kind": "lever", "location": "gone"}]))
        self.enterContext(_no_icons())
        with store.connect() as conn:
            conn.execute("INSERT INTO postings (id, source, title, active, visible) "
                         "VALUES ('old', 'legacy', 'Software Engineer', 1, 1)")

    def _fetch(self, simplify_reply):
        with _served({SIMPLIFY: simplify_reply,
                      "lever": urllib.error.URLError("404")}):
            fetch.fetch_new()
        return store.get_posting("old")["active"]

    def test_a_failing_board_does_not_hold_back_legacy_deactivation(self):
        self.assertFalse(self._fetch("simplify"))

    def test_a_failing_feed_keeps_legacy_rows_active(self):
        self.assertTrue(self._fetch(urllib.error.URLError("503")))


class RenameSourcesTest(unittest.TestCase):
    """Postings stored before sources had names carry the listings URL."""

    OLD_INTERNSHIPS = ("https://raw.githubusercontent.com/SimplifyJobs/Summer2026-Internships/"
                       "dev/.github/scripts/listings.json")

    def setUp(self):
        self.enterContext(temp_home(sources=[{"kind": "github", "location": SIMPLIFY}]))
        self.enterContext(_no_icons())
        rows = _fixture("simplify")
        store.upsert_postings(rows[:2], SIMPLIFY)
        store.upsert_postings(rows[2:], self.OLD_INTERNSHIPS)

    def _sources(self):
        return {r[0]: r[1] for r in store.connect().execute(
            "SELECT id, source FROM postings")}

    def test_rename_is_idempotent(self):
        mapping = {SIMPLIFY: "SimplifyJobs/New-Grad-Positions"}
        store.rename_sources(mapping)
        store.rename_sources(mapping)
        self.assertEqual(sorted(self._sources().values()),
                         ["SimplifyJobs/New-Grad-Positions", "SimplifyJobs/New-Grad-Positions",
                          self.OLD_INTERNSHIPS])

    def test_a_fetch_relabels_url_sources_including_the_renamed_repo(self):
        with _served({SIMPLIFY: [_fixture("simplify")[0]]}):
            fetch.fetch_new()
        by_id = self._sources()
        self.assertEqual(set(by_id.values()), {"SimplifyJobs/New-Grad-Positions",
                                               "SimplifyJobs/Summer2027-Internships"})
        # a relabelled posting the feed no longer lists is delisted under its new name
        self.assertFalse(store.get_posting("5f0e1a2b-0000-4000-8000-000000000002")["active"])


def _text(name):
    return (FIXTURES / name).read_text(encoding="utf-8")


def _served_text(by_fragment):
    """A stand-in for sources._web_text answering with the fixture file whose key is
    in the URL; a reply that is an exception is raised."""
    requested = []

    def web_text(url, deadline, limit=None, cut=False, headers=None):
        requested.append(url)
        for fragment, reply in by_fragment.items():
            if fragment in url:
                if isinstance(reply, Exception):
                    raise reply
                return _text(reply)
        raise AssertionError(f"unexpected request for {url}")
    patch = mock.patch.object(sources, "_web_text", web_text)
    patch.requested = requested
    return patch


SEPTEMBER_THREAD = 49522897
AUGUST_THREAD = 49156683
SEPTEMBER_POSTED = 1788274877


def _hn_served(requested=None):
    """A stand-in for sources._web_json serving the Algolia search, the thread, and
    each comment by its item id."""
    comments = _fixture("hn_comments")

    def web_json(url, deadline, headers=None):
        if requested is not None:
            requested.append(url)
        if "hn.algolia.com" in url:
            return _fixture("hn_search")
        item = url.rsplit("/", 1)[-1].removesuffix(".json")
        if item == str(SEPTEMBER_THREAD):
            return _fixture("hn_story")
        if item == str(AUGUST_THREAD):
            return {"id": AUGUST_THREAD, "kids": [48800001], "type": "story"}
        if item == "48800001":
            return {"id": 48800001, "parent": AUGUST_THREAD, "time": 1785800000,
                    "type": "comment", "text": "Brindle | Backend Engineer | Boston, MA"}
        return comments[item]
    return mock.patch.object(sources, "_web_json", web_json)


class HackerNewsTest(unittest.TestCase):
    def _fetch(self, now, location=sources.HN_LATEST, requested=None):
        with mock.patch("time.time", return_value=now), _hn_served(requested):
            return sources.hn_hiring(location)

    def test_each_top_level_comment_with_fields_is_a_posting(self):
        rows = self._fetch(SEPTEMBER_POSTED + 10 * DAY)
        self.assertEqual([r["id"] for r in rows],
                         ["hn:49522897:49523712", "hn:49522897:49524580"])
        first, second = rows
        self.assertEqual((first["company_name"], first["title"]), ("TALLOWGATE", "Engineer"))
        self.assertEqual(first["locations"], ["Berlin, Germany", "ONSITE (Germany)"])
        self.assertEqual(first["url"], "https://news.ycombinator.com/item?id=49523712")
        self.assertEqual((first["date_posted"], first["category"]), (1788277998, None))
        self.assertEqual((second["company_name"], second["title"]),
                         ("Quillmark Foundation", "Software Engineer, Security"))
        self.assertEqual(second["locations"], ["REMOTE (US + 18 countries)"])

    def test_a_comment_without_fields_or_a_deleted_one_is_skipped(self):
        comments = _fixture("hn_comments")
        self.assertIsNone(sources._hn_row(SEPTEMBER_THREAD, comments["49523176"]))
        self.assertIsNone(sources._hn_row(SEPTEMBER_THREAD, comments["49524098"]))
        self.assertIsNone(sources._hn_row(SEPTEMBER_THREAD, None))

    def test_remote_anywhere_on_the_first_line_names_remote_in_the_locations(self):
        row = sources._hn_row(1, {"id": 2, "time": NOW,
                                  "text": "Fernhollow | Remote Software Engineer | Full-time"})
        self.assertEqual((row["title"], row["locations"]),
                         ("Remote Software Engineer", ["Remote"]))

    def test_the_title_is_the_first_field_naming_a_role_and_urls_leave_the_company(self):
        row = sources._hn_row(1, {"id": 2, "time": NOW, "text":
                                  "Redmoss https://redmoss.example/ | Madrid, Spain | HYBRID | "
                                  "Scientific Computing Engineer | Full-time<p>More."})
        self.assertEqual((row["company_name"], row["title"]),
                         ("Redmoss", "Scientific Computing Engineer"))
        self.assertEqual(row["locations"], ["Madrid, Spain", "HYBRID"])

    def test_a_thread_under_a_week_old_is_read_with_the_month_before(self):
        rows = self._fetch(SEPTEMBER_POSTED + 3 * DAY)
        self.assertEqual([r["id"] for r in rows],
                         ["hn:49522897:49523712", "hn:49522897:49524580",
                          "hn:49156683:48800001"])

    def test_a_thread_url_reads_that_thread_without_a_search(self):
        requested = []
        rows = self._fetch(SEPTEMBER_POSTED + 3 * DAY,
                           "https://news.ycombinator.com/item?id=49156683", requested)
        self.assertEqual([r["id"] for r in rows], ["hn:49156683:48800001"])
        self.assertFalse(any("algolia" in url for url in requested))

    def test_comments_past_the_cap_are_not_requested(self):
        requested = []
        with mock.patch.object(sources, "HN_MAX_COMMENTS", 2):
            rows = self._fetch(SEPTEMBER_POSTED + 10 * DAY, requested=requested)
        self.assertEqual(len(rows), 2)
        self.assertFalse(any("49523176" in url or "49524098" in url for url in requested))


JOBRIGHT = ("https://raw.githubusercontent.com/jobright-ai/2026-Software-Engineer-New-Grad/"
            "master/README.md")
SPEEDYAPPLY = ("https://raw.githubusercontent.com/speedyapply/2027-SWE-College-Jobs/main/"
               "NEW_GRAD_USA.md")
# 2026-09-25 12:00 UTC
README_NOW = 1790337600.0


class GithubReadmeTest(unittest.TestCase):
    def _rows(self, url, fixture):
        with mock.patch("time.time", return_value=README_NOW), \
                _served_text({url: fixture}):
            return sources.github_readme(url)

    def test_rows_carry_ids_from_the_apply_url_and_the_row_above_for_arrows(self):
        rows = self._rows(JOBRIGHT, "readme_jobright.md")
        self.assertEqual([r["id"] for r in rows],
                         [sources._path_safe("github_readme:https://jobright.ai/jobs/info/6ab6aac34873fd3fd852e870"),
                          sources._path_safe("github_readme:https://jobright.ai/jobs/info/6ab6aac19d4843569fe4ebd3"),
                          sources._path_safe("github_readme:https://jobright.ai/jobs/info/6a52c0efe726ec56126a450c")])
        self.assertEqual([r["company_name"] for r in rows],
                         ["Tallowgate Systems", "Tallowgate Systems", "Quillmark Space"])
        self.assertEqual(rows[0]["title"], "Junior Full Stack Developer (Data CoE)")
        self.assertEqual(rows[0]["url"], "https://jobright.ai/jobs/info/6ab6aac34873fd3fd852e870"
                                         "?utm_campaign=Software%20Engineering&utm_source=1103")
        self.assertEqual(rows[1]["locations"], ["Bellevue, WA, United States"])
        self.assertEqual([r["category"] for r in rows], [None, None, None])

    def test_a_month_and_day_is_the_latest_such_date_up_to_tomorrow(self):
        rows = self._rows(JOBRIGHT, "readme_jobright.md")
        self.assertEqual([r["date_posted"] for r in rows],
                         [_unix("2026-09-25T00:00:00+00:00"), _unix("2026-09-24T00:00:00+00:00"),
                          _unix("2025-12-30T00:00:00+00:00")])
        self.assertFalse(any(r.get("date_is_relative") for r in rows))

    def test_a_row_without_a_link_is_skipped(self):
        rows = self._rows(JOBRIGHT, "readme_jobright.md")
        self.assertNotIn("Fernhollow Labs", [r["company_name"] for r in rows])
        rows = self._rows(SPEEDYAPPLY, "readme_speedyapply.md")
        self.assertNotIn("Cobalt Grants", [r["company_name"] for r in rows])

    def test_html_links_section_headings_and_ages(self):
        rows = self._rows(SPEEDYAPPLY, "readme_speedyapply.md")
        self.assertEqual([(r["company_name"], r["category"]) for r in rows],
                         [("Tallowgate", "Software"), ("Redmoss Capital", "Quant"),
                          ("Brindle & Co", "Software")])
        self.assertEqual(rows[1]["url"], "https://job-boards.greenhouse.io/redmoss/jobs/4716932005")
        self.assertEqual(rows[2]["title"], "Software Engineer | Platform")
        self.assertEqual([r["date_posted"] for r in rows],
                         [README_NOW - 20 * DAY, README_NOW, README_NOW - 60 * DAY])
        self.assertTrue(all(r["date_is_relative"] for r in rows))

    def test_markdown_links_in_the_apply_column_and_image_buttons(self):
        text = ("## Software Engineering\n"
                "| Company | Role | Location | Application/Link | Date Posted |\n"
                "| --- | --- | --- | :---: | --- |\n"
                "| [Redmoss](https://redmoss.example) | SWE, New Grad | NYC | "
                "[![Apply](https://img.example/apply.png)](https://redmoss.example/jobs/7) "
                "| 2 days ago |\n")
        (row,) = sources._readme_rows(text, NOW)
        self.assertEqual((row["company_name"], row["url"], row["category"]),
                         ("Redmoss", "https://redmoss.example/jobs/7", "Software"))
        self.assertEqual(row["date_posted"], NOW - 2 * DAY)

    def test_an_arrow_in_a_tables_first_row_names_no_company(self):
        text = ("## Software\n| Company | Role | Link |\n|---|---|---|\n"
                "| Redmoss | SWE | [a](https://redmoss.example/1) |\n\n"
                "## Quant\n| Company | Role | Link |\n|---|---|---|\n"
                "| ↳ | Quant Dev | [b](https://other.example/2) |\n")
        self.assertEqual([r["company_name"] for r in sources._readme_rows(text, NOW)],
                         ["Redmoss"])

    def test_a_table_without_company_and_role_columns_is_ignored(self):
        text = "| Name | Link |\n|---|---|\n| Redmoss | [x](https://redmoss.example) |\n"
        self.assertEqual(sources._readme_rows(text, NOW), [])

    def test_date_cells(self):
        cases = {"5h": (NOW - 5 * 3600, True), "1w": (NOW - 7 * DAY, True),
                 "3 days ago": (NOW - 3 * DAY, True),
                 "Sep 20, 2024": (_unix("2024-09-20T00:00:00+00:00"), False),
                 "soon": (None, False), "": (None, False)}
        for text, expected in cases.items():
            with self.subTest(text):
                self.assertEqual(sources._listed_at(text, NOW), expected)

    def test_a_list_is_named_by_its_repo_and_any_file_other_than_the_readme(self):
        self.assertEqual(sources.Source("github_readme", JOBRIGHT).name,
                         "jobright-ai/2026-Software-Engineer-New-Grad")
        self.assertEqual(sources.Source("github_readme", SPEEDYAPPLY).name,
                         "speedyapply/2027-SWE-College-Jobs/NEW_GRAD_USA.md")


class YCTest(unittest.TestCase):
    def setUp(self):
        self.heard = []
        self.addCleanup(events.subscribe(self.heard.append))
        self.enterContext(mock.patch("time.time", return_value=NOW))
        with _served_text({"software-engineer/san-francisco": "yc_jobs_san_francisco.html",
                           "software-engineer/remote": urllib.error.URLError("reset"),
                           "software-engineer": "yc_jobs.html"}):
            self.rows = sources.yc_waas(sources.YC_JOBS)

    def test_only_jobs_open_to_new_grads_are_kept_once_each_across_pages(self):
        self.assertEqual([r["id"] for r in self.rows],
                         ["yc_waas:97346", "yc_waas:95120", "yc_waas:96001", "yc_waas:97400"])
        self.assertEqual((self.rows[0]["company_name"], self.rows[0]["title"]),
                         ("Tallowgate", "Product Engineer, New Products"))
        self.assertEqual(self.rows[0]["url"], "https://www.ycombinator.com/companies/tallowgate/"
                                              "jobs/x97346-product-engineer,-new-products")

    def test_locations_split_and_dates_count_back(self):
        self.assertEqual(self.rows[1]["locations"], ["New York, NY, US", "Remote (US)"])
        self.assertEqual([r["date_posted"] for r in self.rows],
                         [NOW - 90 * DAY, NOW - 30 * DAY, NOW - 11 * DAY, NOW - 730 * DAY])
        self.assertTrue(all(r["date_is_relative"] for r in self.rows))

    def test_a_city_page_that_fails_is_a_warning(self):
        self.assertEqual([(e.kind, e.data["text"]) for e in self.heard],
                         [("warn", f"[fetch] yc_waas:{sources.YC_JOBS}: 1 of 2 city pages "
                                   "failed to load")])


class RemoteOKTest(unittest.TestCase):
    def test_engineering_jobs_are_kept_and_the_legal_notice_skipped(self):
        with _web_served({"remoteok.com/api": "remoteok"}):
            batches = sources.fetch_all([sources.Source("remoteok", sources.REMOTEOK_API)])
        ((name, rows),) = batches
        self.assertEqual(name, "remoteok:https://remoteok.com/api")
        self.assertEqual([r["id"] for r in rows], ["remoteok:1137427", "remoteok:1137417"])
        self.assertEqual([r["locations"] for r in rows], [["Remote"], ["Remote", "Austin, TX"]])
        self.assertEqual(rows[0]["date_posted"], _unix("2026-09-23T14:00:02+00:00"))
        self.assertEqual((rows[0]["company_name"], rows[0]["title"]),
                         ("Fernhollow", "Software Engineer"))


class USAJobsTest(unittest.TestCase):
    """The fixture follows the documented reply shape; the API needs a key, so it was
    never called live."""

    def test_without_a_key_the_source_is_skipped_with_one_warning(self):
        heard = []
        self.addCleanup(events.subscribe(heard.append))
        self.enterContext(temp_home(usajobs_email="me@example.com"))
        with _web_served({}):
            self.assertEqual(sources.fetch_all([sources.Source("usajobs", "software engineer")]),
                             [])
        self.assertEqual([(e.kind, e.data["text"]) for e in heard],
                         [("warn", "[fetch] skipped usajobs:software engineer: add the USAJOBS "
                                   "API key and usajobs_email in Settings to read USAJOBS")])

    def test_computing_series_are_kept_with_the_stored_key_sent_in_headers(self):
        self.enterContext(temp_home(usajobs_email="me@example.com"))
        secrets.set_key("usajobs", "k3y")
        served = _web_served({"data.usajobs.gov": "usajobs"})
        with served:
            rows = sources.usajobs("software engineer")
        ((url, headers),) = served.asked
        self.assertEqual(url, "https://data.usajobs.gov/api/search?Keyword=software+engineer"
                              "&JobCategoryCode=2210%3B1550%3B0854%3B1515&ResultsPerPage=250")
        self.assertEqual(headers, {"Authorization-Key": "k3y", "User-Agent": "me@example.com"})
        self.assertEqual([r["id"] for r in rows], ["usajobs:812345600", "usajobs:812345601"])
        first = rows[0]
        self.assertEqual((first["company_name"], first["title"]),
                         ("Bureau of Tidewater Surveys", "IT Specialist (APPSW)"))
        self.assertEqual(first["url"], "https://www.usajobs.gov:443/GetJob/ViewDetails/812345600")
        self.assertEqual(first["locations"], ["Washington, District of Columbia",
                                              "Anywhere in the U.S. (remote job)"])
        self.assertEqual(first["date_posted"], _unix("2026-09-15T08:05:12.027+00:00"))


CAREERS = "https://www.tallowgate.example/careers"


class PathSafeIdTest(unittest.TestCase):
    def test_an_id_built_from_a_link_holds_no_slash_and_stays_stable(self):
        link_id = "github_readme:https://www.weareroku.com/jobs/8223823?gh_jid=8223823"
        self.assertNotIn("/", sources._path_safe(link_id))
        self.assertTrue(sources._path_safe(link_id).startswith("github_readme:"))
        self.assertEqual(sources._path_safe(link_id), sources._path_safe(link_id))
        self.assertEqual(sources._path_safe("greenhouse:acme:123"), "greenhouse:acme:123")


class PageTest(unittest.TestCase):
    def setUp(self):
        with _served_text({CAREERS: "page.html"}):
            self.rows = sources.page(CAREERS, "Tallowgate")

    def test_links_naming_a_role_are_postings_once_each(self):
        self.assertEqual([(r["title"], r["url"]) for r in self.rows], [
            ("Software Engineer, New Grad", "https://www.tallowgate.example/careers/jobs/4121"),
            ("Data Analyst", "https://www.tallowgate.example/careers/jobs/4122"),
            ("Research Scientist, Storage", "https://www.tallowgate.example/careers/jobs/4123"),
            ("Engineering Manager, AI Platform", "https://boards.tallowgate.example/jobs/9")])
        self.assertEqual(self.rows[0]["id"],
                         sources._path_safe("page:https://www.tallowgate.example/careers/jobs/4121"))
        self.assertEqual({(r["company_name"], r["date_posted"]) for r in self.rows},
                         {("Tallowgate", None)})

    def test_locations_come_from_the_element_around_the_link_or_inside_a_card(self):
        self.assertEqual([r["locations"] for r in self.rows],
                         [["New York, NY"], ["Austin, TX", "Remote"], ["Seattle, WA"],
                          ["San Francisco, CA", "London, United Kingdom"]])

    def test_links_past_the_cap_are_left_out(self):
        with mock.patch.object(sources, "PAGE_MAX_LINKS", 2), \
                _served_text({CAREERS: "page.html"}):
            self.assertEqual(len(sources.page(CAREERS, "Tallowgate")), 2)

    def test_the_page_is_read_with_its_own_time_limit_and_cut_at_its_size_limit(self):
        asked = []

        def get(url, *, limit, deadline, headers=None, method="GET", data=None):
            asked.append((limit, round(deadline - time.monotonic())))
            return net.Response(b"<a href='/j/1'>Software Engineer</a>" + b" " * limit,
                                "text/html", url, 200)
        with mock.patch.object(net, "get", get):
            rows = sources.page(CAREERS, "Tallowgate")
        self.assertEqual(asked, [(512 * 1024, 20)])
        self.assertEqual([r["url"] for r in rows], ["https://www.tallowgate.example/j/1"])

    def test_ids_keep_only_the_query_parameters_that_name_a_job(self):
        self.assertEqual(
            sources._page_id("https://Acme.example/Jobs/?jobId=R12&utm_source=x&REQ=7"),
            "page:https://acme.example/jobs?jobId=R12&REQ=7")
        self.assertEqual(sources._page_id("https://acme.example/jobs/?utm_source=x"),
                         "page:https://acme.example/jobs")
        page = ('<a href="/open?jobId=1&utm_source=a">Software Engineer</a>'
                '<a href="/open?jobId=1&utm_source=b">Software Engineer</a>'
                '<a href="/open?jobId=2">Data Engineer</a>')
        with mock.patch.object(sources, "_web_text", lambda *args, **kwargs: page):
            rows = sources.page(CAREERS, "Tallowgate")
        self.assertEqual([r["id"] for r in rows],
                         [sources._path_safe("page:https://www.tallowgate.example/open?jobId=1"),
                          sources._path_safe("page:https://www.tallowgate.example/open?jobId=2")])

    def test_hostile_markup_parses_in_linear_time(self):
        pages = {"nested": ("<b>x" * 131072)[:sources.PAGE_MAX_BYTES],
                 "unclosed": ("<span>" * 60000 + "</div>" * 20000)[:sources.PAGE_MAX_BYTES]}
        for name, page in pages.items():
            with self.subTest(name):
                parser = sources._Anchors(CAREERS, time.monotonic() + 20)
                start = time.monotonic()
                parser.parse(page)
                self.assertLess(time.monotonic() - start, 0.5)

    def test_parsing_stops_at_the_tag_cap_and_the_deadline_with_a_warning(self):
        heard = []
        self.addCleanup(events.subscribe(heard.append))
        cards = "".join(f'<li><a href="/j/{i}">Software Engineer {i}</a></li>' for i in range(3))
        page = cards + "<i>" * sources.PAGE_MAX_TAGS + '<a href="/j/late">Data Engineer</a>'
        with mock.patch.object(sources, "_web_text", lambda *args, **kwargs: page):
            rows = sources.page(CAREERS, "Tallowgate")
        self.assertEqual(len(rows), 3)
        self.assertEqual([e.data["text"] for e in heard],
                         [f"[fetch] page:{CAREERS}: read part of the page, over 50000 tags"])
        parser = sources._Anchors(CAREERS, time.monotonic() - 1)
        parser.parse("<i>" * 1000)
        self.assertEqual(parser.cut_short, "out of time")


class WebReadTest(unittest.TestCase):
    """Feeds read through net with a size cap and the source's deadline."""

    def _serve(self, body):
        asked = []

        def get(url, *, limit, deadline, headers=None, method="GET", data=None):
            asked.append((url, limit, round(deadline - time.monotonic())))
            return net.Response(body, "application/json", url, 200)
        self.enterContext(mock.patch.object(net, "get", get))
        return asked

    def test_a_json_feed_is_read_up_to_2_mib_within_the_fetch_timeout(self):
        asked = self._serve(b"[]")
        self.assertEqual(sources.remoteok(sources.REMOTEOK_API), [])
        self.assertEqual(asked, [(sources.REMOTEOK_API, 2 * 1024 * 1024, 60)])

    def test_a_reply_over_the_cap_fails(self):
        self._serve(b" " * (2 * 1024 * 1024 + 1))
        with self.assertRaisesRegex(ValueError, "reply over 2048 KiB"):
            sources.github_readme(SPEEDYAPPLY)

    def test_yc_city_pages_off_www_ycombinator_com_are_never_read(self):
        asked = []

        def props(url, deadline):
            asked.append(url)
            return {"sidebarLinks": [["Remote", "/jobs/role/software-engineer/remote"],
                                     ["Elsewhere", "https://jobs.elsewhere.example/x"],
                                     ["Other", "https://ycombinator.com.evil.example/jobs"]]}
        with mock.patch.object(sources, "_inertia_props", props):
            sources.yc_waas(sources.YC_JOBS)
        self.assertEqual(asked, [sources.YC_JOBS,
                                 "https://www.ycombinator.com/jobs/role/software-engineer/remote"])


class SourceForUrlTest(unittest.TestCase):
    def test_urls_name_their_source_without_a_request(self):
        cases = {
            "https://news.ycombinator.com/item?id=49522897":
                ("hn_hiring", "https://news.ycombinator.com/item?id=49522897"),
            "https://www.workatastartup.com/companies?role=eng": ("yc_waas", sources.YC_JOBS),
            "https://www.ycombinator.com/jobs/role/software-engineer/seattle/":
                ("yc_waas", "https://www.ycombinator.com/jobs/role/software-engineer/seattle"),
            "remoteok.com": ("remoteok", sources.REMOTEOK_API),
            "https://remoteok.com/remote-dev-jobs": ("remoteok", sources.REMOTEOK_API),
            SPEEDYAPPLY: ("github_readme", SPEEDYAPPLY),
            "https://github.com/speedyapply/2027-SWE-College-Jobs/blob/main/NEW_GRAD_USA.md":
                ("github_readme", SPEEDYAPPLY),
            SIMPLIFY: ("github", SIMPLIFY),
            "https://jobs.lever.co/figma": ("lever", "figma"),
        }
        patch = _net_served({})
        with patch:
            for url, expected in cases.items():
                with self.subTest(url):
                    found = sources.source_for_url(url)
                    self.assertEqual((found.kind, found.location), expected)
        self.assertEqual(patch.requested, [])

    def test_a_repo_url_is_its_feed_when_it_has_one_else_its_readme(self):
        patch = _net_served({"SimplifyJobs/New-Grad-Positions/HEAD/.github/scripts/listings.json":
                          b""})
        with patch:
            feed = sources.source_for_url("https://github.com/SimplifyJobs/New-Grad-Positions")
            readme = sources.source_for_url(
                "https://github.com/jobright-ai/2026-Software-Engineer-New-Grad")
        self.assertEqual((feed.kind, feed.location),
                         ("github", "https://raw.githubusercontent.com/SimplifyJobs/"
                                    "New-Grad-Positions/HEAD/.github/scripts/listings.json"))
        self.assertEqual((readme.kind, readme.location),
                         ("github_readme", "https://raw.githubusercontent.com/jobright-ai/"
                                           "2026-Software-Engineer-New-Grad/HEAD/README.md"))
        self.assertEqual(len(patch.requested), 2)

    def test_any_web_url_is_a_page_only_when_asked_for(self):
        self.assertIsNone(sources.source_for_url(CAREERS))
        page = sources.source_for_url(CAREERS, kind="page")
        self.assertEqual((page.kind, page.location, page.company), ("page", CAREERS, None))
        self.assertIsNone(sources.source_for_url("mailto:jobs@tallowgate.example", kind="page"))

    def test_urls_that_name_nothing_or_another_kind_resolve_to_nothing(self):
        with _net_served({}):
            for url, kind in [("https://news.ycombinator.com/newest", None),
                              ("https://news.ycombinator.com/item?id=abc", None),
                              ("https://www.ycombinator.com/companies", None),
                              ("https://jobs.lever.co/figma", "greenhouse")]:
                with self.subTest(url):
                    self.assertIsNone(sources.source_for_url(url, kind))

    def test_resolve_company_still_finds_only_boards(self):
        with _net_served({}):
            self.assertIsNone(sources.resolve_company(
                "https://news.ycombinator.com/item?id=49522897"))


class NewKindSettingsTest(unittest.TestCase):
    def setUp(self):
        self.enterContext(temp_home())

    def _load(self, text):
        paths.config_file().write_text(text, encoding="utf-8")
        return settings.load()

    def test_every_new_kind_and_the_usajobs_account_round_trip(self):
        original = dataclasses.replace(
            settings.defaults(), usajobs_email="me@example.com",
            sources=[{"kind": "github_readme", "location": SPEEDYAPPLY},
                     {"kind": "hn_hiring", "location": "whoishiring"},
                     {"kind": "hn_hiring",
                      "location": "https://news.ycombinator.com/item?id=49522897"},
                     {"kind": "yc_waas", "location": sources.YC_JOBS},
                     {"kind": "remoteok", "location": sources.REMOTEOK_API},
                     {"kind": "usajobs", "location": "software engineer"},
                     {"kind": "page", "location": CAREERS, "company": "Tallowgate"}])
        settings.save(original)
        self.assertEqual(settings.load(), original)

    def test_a_bad_new_kind_spec_is_rejected(self):
        cases = {
            '[[sources]]\nkind = "page"\nlocation = "' + CAREERS + '"\n': "needs a company",
            '[[sources]]\nkind = "page"\nlocation = "careers.example"\ncompany = "X"\n':
                "http(s) URL",
            '[[sources]]\nkind = "linkedin"\nlocation = "x"\n': "kind should be one of",
            '[[sources]]\nkind = "github_readme"\nlocation = "https://github.com/o/r"\n':
                "Markdown file",
            '[[sources]]\nkind = "hn_hiring"\nlocation = "latest"\n': "thread URL",
            '[[sources]]\nkind = "yc_waas"\nlocation = "https://example.com/jobs"\n':
                "www.ycombinator.com/jobs",
            '[[sources]]\nkind = "remoteok"\nlocation = "https://example.com/api"\n':
                "remoteok.com",
            '[[watchlist]]\nkind = "hn_hiring"\nlocation = "whoishiring"\n': "watchlist[0]",
            'usajobs_email = 5\n': "usajobs_email",
        }
        for text, message in cases.items():
            with self.subTest(text):
                with self.assertRaises(settings.SettingsError) as caught:
                    self._load(text)
                self.assertIn(message, str(caught.exception))

    def test_the_default_sources_include_the_verified_readme_list(self):
        self.assertIn({"kind": "github_readme", "location": SPEEDYAPPLY},
                      settings.defaults().sources)
        settings.from_dict({"sources": settings.DEFAULT_SOURCES})


if __name__ == "__main__":
    unittest.main()
