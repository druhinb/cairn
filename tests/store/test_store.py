"""The SQLite store: the whole feed, and everything that happens to a posting.

The relevance rule exists twice, as fetch._relevant and as store.relevant_query.
The UI filters in SQL and the pipeline was built on the Python rule, so the two
are checked against each other on a fixture that reaches every branch.
"""
import ctypes
import dataclasses
import datetime
import json
import os
import re
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from helpers import temp_home, user_home

from cairn import sources, store
from cairn.core import paths, settings
from cairn.jobs import fetch, logos

NOW = time.time()
DAY = 86400

# the two tables schema version 3 had and version 4 drops
V3_RESUME_TABLES = """
CREATE TABLE resumes (signature TEXT PRIMARY KEY, pdf_path TEXT, keywords TEXT,
                      pool_fingerprint TEXT, company TEXT, built_from TEXT, built_at TEXT);
CREATE TABLE tailored (posting_id TEXT PRIMARY KEY REFERENCES postings, pdf_path TEXT,
                       reused_from TEXT, built_at TEXT);
"""

# the two tables whose shape version 5 changed, as version 4 had them
V4_TABLES = """
CREATE TABLE scores (posting_id TEXT PRIMARY KEY REFERENCES postings, fit INTEGER,
                     tier INTEGER, below_floor INTEGER, reason TEXT, scored_at TEXT);
CREATE TABLE applications (posting_id TEXT PRIMARY KEY REFERENCES postings,
                           applied_at TEXT, note TEXT);
"""

# logos as version 7 had it, before source
V7_LOGOS = """
CREATE TABLE IF NOT EXISTS logos (
    domain TEXT PRIMARY KEY,
    path TEXT,
    fetched_at REAL,
    error TEXT
)"""

# postings and descriptions as version 8 had them, before groups, link checks, and
# the salary and sponsorship a summary states
V8_TABLES = """
CREATE TABLE postings (id TEXT PRIMARY KEY, source TEXT, company TEXT, title TEXT, url TEXT,
                       url_key TEXT, locations TEXT, category TEXT, terms TEXT,
                       degrees TEXT, sponsorship TEXT, company_url TEXT, active INTEGER,
                       visible INTEGER, posted_at REAL, updated_at REAL,
                       first_seen_at TEXT, last_seen_at TEXT);
CREATE TABLE descriptions (posting_id TEXT PRIMARY KEY REFERENCES postings, text TEXT,
                           source TEXT, error TEXT, keywords TEXT, fetched_at TEXT);
"""

# application_events as version 9 had it, before stages
V9_EVENTS = """CREATE TABLE IF NOT EXISTS application_events (
    id INTEGER PRIMARY KEY,
    posting_id TEXT REFERENCES postings,
    status TEXT,
    at TEXT,
    note TEXT
);"""


def _v9_schema():
    """store.SCHEMA as version 9 had it, without stages, checklists, or files."""
    schema = re.sub(r"CREATE TABLE IF NOT EXISTS application_events \(.*?\);", V9_EVENTS,
                    store.SCHEMA, flags=re.S)
    for gone in (store.schema._CHECKLIST + ";", store.schema._ATTACHMENTS + ";",
                 "CREATE INDEX IF NOT EXISTS checklist_posting ON checklist(posting_id);"):
        schema = schema.replace(gone, "")
    return schema


def _in(days=0, hours=0):
    """A local ISO time days and hours from now, in store.db.now()'s format."""
    return (datetime.datetime.now() + datetime.timedelta(days=days, hours=hours)).isoformat(
        timespec="seconds")


# scores as versions 5 to 10 had it, with one reason for both axes
V10_SCORES = """
CREATE TABLE IF NOT EXISTS scores (
    posting_id TEXT PRIMARY KEY REFERENCES postings,
    fit INTEGER,
    tier INTEGER,
    below_floor INTEGER,
    reason TEXT,
    scored_at TEXT,
    run_id INTEGER REFERENCES runs
)"""


# postings as version 5 had it, before company_url
V5_POSTINGS = """
CREATE TABLE postings (id TEXT PRIMARY KEY, source TEXT, company TEXT, title TEXT, url TEXT,
                       url_key TEXT, locations TEXT, category TEXT, terms TEXT,
                       degrees TEXT, sponsorship TEXT, active INTEGER, visible INTEGER,
                       posted_at REAL, updated_at REAL, first_seen_at TEXT,
                       last_seen_at TEXT);
"""


def _row(posting_id, title="Software Engineer", company="Acme", **fields):
    row = {"id": posting_id, "company_name": company, "title": title,
           "url": f"https://jobs.test/{posting_id}", "locations": ["Seattle, WA"],
           "category": "Software", "terms": [], "degrees": [], "sponsorship": "Offers Sponsorship",
           "active": True, "is_visible": True, "date_posted": NOW, "date_updated": NOW}
    row.update(fields)
    return row


def _fixture():
    """Postings that between them take every branch of the relevance rule."""
    rows = [
        _row("plain"),
        _row("upper", "SOFTWARE ENGINEER"),
        _row("inactive", active=False),
        _row("hidden", is_visible=False),
        _row("visibility-unstated"),
        _row("senior", "Senior Software Engineer"),
        _row("sr-dot", "Sr. Software Engineer"),
        _row("staff", "Staff Engineer"),
        _row("mechanical", "Mechanical Engineer"),
        _row("photonics", "Photonics Engineer 1"),
        _row("intern-fall", "Software Engineer Intern", terms=["Fall 2026"]),
        _row("intern-summer", "Software Engineer Intern", terms=["Summer 2026"]),
        _row("intern-no-terms", "Software Engineer Intern", terms=[]),
        _row("intern-null-terms", "Software Engineer Intern", terms=None),
        _row("intern-padded", "Software Engineer Intern", terms=["  FALL 2026  "]),
        _row("intern-mixed", "Software Engineer Intern", terms=["Summer 2026", "Fall 2026"]),
        _row("co-op-spring", "Software Engineering Co-op", terms=["Spring 2027"]),
        _row("internship-platform", "Internship - Platform", terms=["Winter 2027"]),
        _row("hardware-cat", category="Hardware"),
        _row("null-cat", category=None),
        _row("empty-cat", category=""),
        _row("meteorologist", "Meteorologist", category="AI/ML/Data"),
        _row("data-analyst", "Data Analyst 2", category="AI/ML/Data"),
        _row("ml", "ML Engineer", category="AI/ML/Data"),
        _row("quant", "Quantitative Researcher", category="Quant"),
        _row("backend", "Backend Developer", category="Software Engineering"),
        _row("null-title", None),
        _row("grad-only", degrees=["Master's", "PhD"]),
        _row("bachelors-ok", degrees=["Bachelor's", "Master's"]),
        _row("degrees-null", degrees=None),
        _row("phd-trader", "Trader", category="Quant", degrees=["PhD"]),
        _row("remote", locations=["Remote in USA"]),
        _row("new-york", locations=["New York, NY"]),
        _row("no-locations", locations=[]),
        _row("null-locations", locations=None),
        _row("split-location", locations=["Seattle", "WA"]),
        _row("austin-sre", "Site Reliability Engineer", locations=["Austin, TX"]),
        _row("firmware", "Graduate Firmware Engineer", category="Hardware"),
        _row("full-stack", "Full Stack Engineer", locations=["Remote"]),
        _row("platform", "Platform Engineer", category="Software"),
        _row("zurich", locations=["Zürich, CH"]),
        _row("zurich-upper", "SOFTWARE ENGINEER", locations=["ZÜRICH"]),
        _row("accented-senior", "SÉNIOR SOFTWARE ENGINEER"),
        _row("tab-term", "Software Engineer Intern", terms=["Fall 2026\t"]),
    ]
    rows[4].pop("is_visible")
    for row in rows:
        if row["id"].startswith("null-cat"):
            row.pop("category")
    return rows


CONFIGS = {
    "defaults": {},
    "narrowed": {"wanted_intern_terms": ["Fall 2026", "spring 2027"],
                 "degrees_held": ["Bachelor's"], "location_allow": ["WA", "Remote"]},
    "no-off-season": {"wanted_intern_terms": ["Fall 2026"],
                      "include_off_season_internships": False},
    "non-ascii": {"wanted_intern_terms": ["Fall 2026"], "location_allow": ["Zürich"]},
    "empty-lists": {"allowed_categories": [], "title_keywords": [], "title_exclude": [],
                    "title_exclude_field": [], "intern_terms": []},
}


class RelevantQueryTest(unittest.TestCase):
    def test_sql_and_python_rules_agree(self):
        for name, overrides in CONFIGS.items():
            with self.subTest(name), temp_home(**overrides):
                rows = _fixture()
                store.upsert_postings(rows, "fixture")
                where, params = store.relevant_query(settings.get())
                by_sql = {r["id"] for r in store.connect().execute(
                    f"SELECT id FROM postings WHERE {where}", params)}
                by_python = {r["id"] for r in rows if fetch._relevant(r)}
                self.assertEqual(by_sql, by_python)
                if name != "empty-lists":
                    self.assertTrue(0 < len(by_python) < len(rows))

    def test_fixture_reaches_both_outcomes_of_each_setting(self):
        with temp_home(**CONFIGS["narrowed"]):
            kept = {r["id"] for r in _fixture() if fetch._relevant(r)}
        for posting_id in ("plain", "intern-fall", "intern-padded", "co-op-spring",
                           "null-cat", "bachelors-ok", "remote", "split-location"):
            self.assertIn(posting_id, kept)
        for posting_id in ("hidden", "senior", "intern-summer", "hardware-cat",
                           "grad-only", "new-york", "null-title"):
            self.assertNotIn(posting_id, kept)


class StoredRelevanceTest(unittest.TestCase):
    """The relevant column follows the rule through upserts, settings changes, and
    the migration that adds it."""

    def setUp(self):
        self.enterContext(temp_home())

    def _stored(self):
        return {r["id"] for r in store.connect().execute(
            "SELECT id FROM postings WHERE relevant = 1")}

    def _listed(self):
        rows, _ = store.search()
        return {job["id"] for job in rows}

    def test_an_upsert_stores_whether_each_posting_passes(self):
        store.upsert_postings([_row("swe"), _row("senior", "Senior Software Engineer")],
                              "feed")
        self.assertEqual(self._stored(), {"swe"})
        store.upsert_postings([_row("swe", "Staff Software Engineer"),
                               _row("senior", "Software Engineer")], "feed")
        self.assertEqual(self._stored(), {"senior"})
        self.assertEqual(self._listed(), {"senior"})

    def test_new_settings_are_applied_before_and_after_the_column_is_recomputed(self):
        store.upsert_postings([_row("swe"), _row("designer", "Product Designer")], "feed")
        self.assertEqual(self._listed(), {"swe"})
        settings.use(dataclasses.replace(settings.get(), title_keywords=["designer"]))
        self.assertEqual(self._listed(), {"designer"})
        store.sync_relevance()
        self.assertEqual(self._stored(), {"designer"})
        self.assertEqual(self._listed(), {"designer"})
        self.assertEqual(store.relevant_query(settings.get())[1], [])

    def test_a_version_11_database_gains_the_column_computed(self):
        # sqlite 3.47 fails DROP COLUMN on this table with "incomplete input", tripped
        # by the comment above the column, so the version 11 table is created without it
        schema_v11, dropped = re.subn(r",\n(?:\s*--[^\n]*\n)*\s*relevant INTEGER", "",
                                      store.SCHEMA)
        self.assertEqual(dropped, 1)
        v11 = sqlite3.connect(paths.db_file())
        v11.executescript(schema_v11 + """
            INSERT INTO meta VALUES ('schema_version', '11');
            INSERT INTO postings (id, title, category, active, visible, posted_at)
                VALUES ('swe', 'Software Engineer', 'Software', 1, 1, unixepoch()),
                       ('senior', 'Senior Software Engineer', 'Software', 1, 1, unixepoch());
        """)
        v11.close()
        self.assertEqual(store.counts()["schema_version"], store.SCHEMA_VERSION)
        self.assertEqual(self._stored(), {"swe"})
        self.assertEqual(self._listed(), {"swe"})


SQLITE_STATUS_MEMORY_USED = 0


class MemoryStatisticsTest(unittest.TestCase):
    def test_sqlite_counts_no_allocations(self):
        status = store.sqlite_library().sqlite3_status64
        status.argtypes = [ctypes.c_int, ctypes.POINTER(ctypes.c_int64),
                           ctypes.POINTER(ctypes.c_int64), ctypes.c_int]
        used, peak = ctypes.c_int64(), ctypes.c_int64()
        with temp_home():
            store.upsert_postings([_row("swe")], "feed")
            self.assertEqual(status(SQLITE_STATUS_MEMORY_USED, ctypes.byref(used),
                                    ctypes.byref(peak), 0), 0)
        self.assertEqual(used.value, 0)

    @unittest.skipIf(sys.platform == "win32", "Windows Pythons load SQLite from sqlite3.dll")
    def test_a_python_with_sqlite_built_in_turns_the_statistics_off_without_a_warning(self):
        # uv's Pythons link _sqlite3 into the interpreter, so the module has no file
        opened = []

        def cdll(name):
            opened.append(name)
            return mock.Mock(**{f"sqlite3_{call}.return_value": 0
                                for call in ("config", "shutdown", "initialize")})
        with mock.patch.object(store.db, "_sqlite3", mock.Mock(spec=[])), \
                mock.patch.object(store.db.ctypes, "CDLL", cdll), \
                mock.patch.object(store.db.ui, "warn") as warn:
            store.db._stop_memory_stats()
        self.assertEqual(opened, [None])
        warn.assert_not_called()


class StoreTestCase(unittest.TestCase):
    def setUp(self):
        self.home = self.enterContext(temp_home())
        self.conn = store.connect()


class SchemaTest(StoreTestCase):
    def test_every_table_exists(self):
        names = {r[0] for r in self.conn.execute("SELECT name FROM sqlite_master")}
        for table in ("meta", "postings", "scores", "descriptions", "applications",
                      "application_events", "seen", "runs", "postings_fts", "logos", "companies"):
            self.assertIn(table, names)
        self.assertFalse({"resumes", "tailored"} & names)

    def test_schema_version_is_recorded(self):
        row = self.conn.execute("SELECT value FROM meta WHERE key = 'schema_version'")
        self.assertEqual(row.fetchone()[0], str(store.SCHEMA_VERSION))

    def test_a_version_1_database_gains_the_log_offsets(self):
        store.close()
        paths.db_file().unlink()
        v1 = sqlite3.connect(paths.db_file())
        v1.executescript(
            "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);"
            "INSERT INTO meta VALUES ('schema_version', '1');"
            "CREATE TABLE runs (id INTEGER PRIMARY KEY, started_at TEXT, finished_at TEXT,"
            " status TEXT, counts TEXT, log_path TEXT);"
            "INSERT INTO runs (status, log_path) VALUES ('ok', '/old/run.log');")
        v1.close()
        conn = store.connect()
        version = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'")
        self.assertEqual(version.fetchone()[0], str(store.SCHEMA_VERSION))
        store.finish_run(1, "ok", {}, log_start=5, log_end=9)
        run = store.list_runs(1)[0]
        self.assertEqual((run["log_path"], run["log_start"], run["log_end"]),
                         ("/old/run.log", 5, 9))

    def test_a_version_2_database_gains_url_keys_that_keep_gh_jid(self):
        """Version 2 dropped the whole query string, gh_jid included."""
        url = "https://careers.acme.example/jobs/search?gh_jid=41"
        store.upsert_postings([_row("gh", url=url)], "feed")
        with self.conn:
            self.conn.execute("UPDATE postings SET url_key = ?",
                              ("https://careers.acme.example/jobs/search",))
            self.conn.execute("UPDATE meta SET value = '2' WHERE key = 'schema_version'")
        store.close()
        store.connect()
        store.upsert_postings([_row("gh-again", url=url)], "other feed")
        self.assertEqual(
            [tuple(r) for r in store.connect().execute("SELECT id, url_key FROM postings")],
            [("gh", "https://careers.acme.example/jobs/search?gh_jid=41")])

    def test_a_version_3_database_loses_the_resume_tables(self):
        store.close()
        paths.db_file().unlink()
        v3 = sqlite3.connect(paths.db_file())
        v3.executescript(V4_TABLES + store.SCHEMA + V3_RESUME_TABLES + """
            INSERT INTO meta VALUES ('schema_version', '3');
            INSERT INTO postings (id, company, title) VALUES ('a', 'Acme', 'SWE');
            INSERT INTO applications VALUES ('a', '2026-09-01T09:00:00', 'ref');
            INSERT INTO seen VALUES ('a', '2026-09-01T07:00:00');
            INSERT INTO resumes (signature, pdf_path) VALUES ('swe', '/r.pdf');
            INSERT INTO tailored (posting_id, pdf_path) VALUES ('a', '/a.pdf');
        """)
        v3.close()
        conn = store.connect()
        self.assertEqual(store.counts()["schema_version"], store.SCHEMA_VERSION)
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master")}
        self.assertFalse({"resumes", "tailored"} & names)
        self.assertEqual(store.get_posting("a")["company_name"], "Acme")
        self.assertEqual(store.application("a")["note"], "ref")
        self.assertEqual(store.seen_ids(), {"a"})

    def test_a_version_4_database_turns_applications_into_statuses(self):
        store.close()
        paths.db_file().unlink()
        v4 = sqlite3.connect(paths.db_file())
        v4.executescript(V4_TABLES + store.SCHEMA + """
            INSERT INTO meta VALUES ('schema_version', '4');
            INSERT INTO postings (id, company, title) VALUES ('a', 'Acme', 'SWE');
            INSERT INTO postings (id, company, title) VALUES ('b', 'Globex', 'SRE');
            INSERT INTO applications VALUES ('a', '2026-09-01T09:00:00', 'ref');
            INSERT INTO applications VALUES ('b', '2026-09-02T10:00:00', '');
            INSERT INTO scores VALUES ('a', 80, 70, 0, 'ok', '2026-09-01T07:00:00');
        """)
        v4.close()
        self.assertEqual(store.counts()["schema_version"], store.SCHEMA_VERSION)
        self.assertEqual(store.get_posting("a")["fit_reason"], "ok")
        a = store.application("a")
        self.assertEqual((a["status"], a["applied_at"], a["created_at"], a["updated_at"],
                          a["note"], a["fit"]),
                         ("applied", "2026-09-01T09:00:00", "2026-09-01T09:00:00",
                          "2026-09-01T09:00:00", "ref", 80))
        self.assertEqual(store.application("b")["applied_at"], "2026-09-02T10:00:00")
        for event_id, posting_id, at in ((1, "a", "2026-09-01T09:00:00"),
                                         (2, "b", "2026-09-02T10:00:00")):
            self.assertEqual(store.application_events(posting_id),
                             [{"id": event_id, "status": "applied", "stage": None, "at": at,
                               "note": None}])
        self.assertEqual(store.counts()["applications"]["applied"], 2)
        names = {r[0] for r in store.connect().execute("SELECT name FROM sqlite_master")}
        self.assertFalse({"applications_v4", "scores_v4"} & names)

    def test_a_version_5_database_gains_company_urls_and_logos(self):
        store.close()
        paths.db_file().unlink()
        v5 = sqlite3.connect(paths.db_file())
        v5.executescript(V5_POSTINGS + store.SCHEMA.replace(store.schema._LOGOS + ";", "")
                         .replace(store.schema._COMPANIES + ";", "") + """
            INSERT INTO meta VALUES ('schema_version', '5');
            INSERT INTO postings (id, company, title, active, visible)
                VALUES ('a', 'Acme', 'SWE', 1, 1);
        """)
        tables = {row[0] for row in v5.execute("SELECT name FROM sqlite_master")}
        self.assertFalse({"logos", "companies"} & tables)
        v5.close()
        self.assertEqual(store.counts()["schema_version"], store.SCHEMA_VERSION)
        self.assertEqual(store.get_posting("a")["company_name"], "Acme")
        self.assertIsNone(store.get_posting("a")["company_url"])
        store.upsert_postings([_row("a", company_url="https://acme.example")], "feed")
        self.assertEqual(store.get_posting("a")["company_url"], "https://acme.example")
        store.save_logo("acme.example", None, "404")
        self.assertEqual(store.logo("acme.example")["error"], "404")

    def test_a_version_6_database_gains_companies(self):
        store.close()
        paths.db_file().unlink()
        v6 = sqlite3.connect(paths.db_file())
        v6.executescript(store.SCHEMA.replace(store.schema._COMPANIES + ";", "") + """
            INSERT INTO meta VALUES ('schema_version', '6');
            INSERT INTO postings (id, company, title, active, visible)
                VALUES ('a', 'Acme', 'SWE', 1, 1);
        """)
        v6.close()
        self.assertEqual(store.counts()["schema_version"], store.SCHEMA_VERSION)
        store.save_company("acme", "acme.example", "feed")
        self.assertEqual(store.company_domain("acme"), "acme.example")

    def test_a_version_7_database_keeps_its_icons_and_records_their_source(self):
        store.close()
        paths.db_file().unlink()
        v7 = sqlite3.connect(paths.db_file())
        v7.executescript(store.SCHEMA.replace(store.schema._LOGOS, V7_LOGOS) + """
            INSERT INTO meta VALUES ('schema_version', '7');
            INSERT INTO logos VALUES ('acme.example', '/old/home/logos/acme.example.png', 1.0,
                                      NULL);
            INSERT INTO logos VALUES ('broken.example', NULL, 2.0, 'HTTP 404');
        """)
        v7.close()
        self.assertEqual(store.counts()["schema_version"], store.SCHEMA_VERSION)
        self.assertEqual(store.logos(), {
            "acme.example": {"domain": "acme.example", "path": "acme.example.png",
                             "fetched_at": 1.0, "error": None, "source": "site"},
            "broken.example": {"domain": "broken.example", "path": None, "fetched_at": 2.0,
                               "error": "HTTP 404", "source": None}})
        store.save_logo("globex.example", self.home / "logos" / "globex.example.png",
                        source="gstatic")
        self.assertEqual({k: v for k, v in store.logo("globex.example").items()
                          if k != "fetched_at"},
                         {"domain": "globex.example", "path": "globex.example.png",
                          "error": None, "source": "gstatic"})

    def test_a_version_8_database_groups_its_postings(self):
        store.close()
        paths.db_file().unlink()
        v8 = sqlite3.connect(paths.db_file())
        v8.executescript(V8_TABLES + store.SCHEMA + """
            INSERT INTO meta VALUES ('schema_version', '8');
            INSERT INTO postings (id, source, company, title, active, visible, first_seen_at)
                VALUES ('a', 'one', 'Acme', 'SWE', 1, 1, '2026-09-01'),
                       ('b', 'two', 'ACME ', 'S.W.E.', 1, 1, '2026-09-02'),
                       ('c', 'one', 'Acme', 'SWE', 1, 1, '2026-09-03');
            INSERT INTO descriptions VALUES ('a', 'text', 'html', NULL, 'TECH: Go', 'now');
        """)
        v8.close()
        self.assertEqual(store.counts()["schema_version"], store.SCHEMA_VERSION)
        groups = dict(store.connect().execute("SELECT id, group_key FROM postings"))
        self.assertEqual(groups, {"a": "acme\nswe\n#1", "b": "acme\ns w e\n#1",
                                  "c": "acme\nswe\n#2"})
        store.set_keywords("a", "TECH: Go\nSALARY: USD 100,000 / year",
                           {"min": 100000, "max": 100000, "period": "year", "currency": "USD"},
                           True)
        self.assertEqual(store.get_posting("a")["salary"],
                         {"min": 100000, "max": 100000, "period": "year", "currency": "USD"})
        self.assertEqual(store.get_posting("a")["offers_sponsorship"], "yes")

    def test_a_version_9_database_keeps_its_history_and_gains_stages(self):
        store.close()
        paths.db_file().unlink()
        v9 = sqlite3.connect(paths.db_file())
        # some version 9 databases were written before salary_currency existed
        v9.executescript(_v9_schema().replace("    salary_currency TEXT,\n", "") + """
            INSERT INTO meta VALUES ('schema_version', '9');
            INSERT INTO postings (id, company, title, active, visible)
                VALUES ('a', 'Acme', 'SWE', 1, 1);
            INSERT INTO applications VALUES ('a', 'applied', '2026-09-01T09:00:00', 'ref',
                                             '2026-09-01T09:00:00', '2026-09-01T09:00:00');
            INSERT INTO application_events (posting_id, status, at, note)
                VALUES ('a', 'applied', '2026-09-01T09:00:00', 'ref');
        """)
        tables = {row[0] for row in v9.execute("SELECT name FROM sqlite_master")}
        self.assertFalse({"checklist", "attachments"} & tables)
        v9.close()
        self.assertEqual(store.counts()["schema_version"], store.SCHEMA_VERSION)
        self.assertEqual(store.application_events("a"), [
            {"id": 1, "status": "applied", "stage": None, "at": "2026-09-01T09:00:00",
             "note": "ref"}])
        self.assertEqual((store.application("a")["status"], store.application("a")["note"]),
                         ("applied", "ref"))
        self.assertEqual(store.add_stage("a", "screen", "2026-09-10T10:00")["stage"], "screen")
        self.assertEqual(store.add_item("a", "Portfolio")["position"], 0)
        self.assertEqual(store.application("a")["checklist"], {"done": 0, "total": 1})
        self.assertIsNone(store.get_posting("a")["salary"])

    def test_a_version_10_database_keeps_its_reason_as_the_fit_reason(self):
        store.close()
        paths.db_file().unlink()
        v10 = sqlite3.connect(paths.db_file())
        v10.executescript(store.SCHEMA.replace(store.schema._SCORES, V10_SCORES) + """
            INSERT INTO meta VALUES ('schema_version', '10');
            INSERT INTO postings (id, company, title, active, visible)
                VALUES ('a', 'Acme', 'SWE', 1, 1), ('b', 'Globex', 'SRE', 1, 1);
            INSERT INTO scores VALUES ('a', 80, 40, 1, 'backend role, weak tier',
                                       '2026-09-01T07:00:00', NULL);
            INSERT INTO scores VALUES ('b', 60, 70, 0, NULL, '2026-09-01T07:00:00', NULL);
        """)
        v10.close()
        self.assertEqual(store.counts()["schema_version"], store.SCHEMA_VERSION)
        a, b = store.get_posting("a"), store.get_posting("b")
        self.assertEqual((a["fit"], a["tier"], a["below_floor"], a["fit_reason"],
                          a["tier_reason"]), (80, 40, True, "backend role, weak tier", None))
        self.assertEqual((b["fit_reason"], b["tier_reason"]), (None, None))
        store.save_reasons("b", "Go services match.", "Solid infra team.")
        self.assertEqual(store.get_posting("b")["tier_reason"], "Solid infra team.")

    def test_database_lives_in_the_home_directory(self):
        self.assertTrue((self.home / "pipeline.db").exists())
        self.assertEqual(paths.db_file(), self.home / "pipeline.db")

    def test_foreign_keys_are_enforced(self):
        with self.assertRaises(sqlite3.IntegrityError):
            store.set_status("no-such-posting", "saved")


class ThreadTest(StoreTestCase):
    def test_a_worker_thread_writes_and_the_main_thread_reads(self):
        opened = []

        def write():
            store.upsert_postings([_row("from-worker")], "feed")
            opened.append(store.connect())

        worker = threading.Thread(target=write)
        worker.start()
        worker.join(timeout=10)
        self.assertEqual(store.get_posting("from-worker")["company_name"], "Acme")
        self.assertIsNot(opened[0], self.conn)

        store.close()
        path = paths.db_file().resolve()
        self.assertEqual([k for k in store.db._connections if k[0] == path], [])
        for conn in (opened[0], self.conn):
            with self.assertRaises(sqlite3.ProgrammingError):
                conn.execute("SELECT 1")


class UpsertTest(StoreTestCase):
    def _found(self, posting_id):
        return self.conn.execute("SELECT found_at FROM postings WHERE id = ?",
                                 (posting_id,)).fetchone()[0]

    def test_only_what_appears_after_a_sources_first_read_is_found(self):
        store.upsert_postings([_row("a")], "feed")
        store.upsert_postings([_row("a"), _row("b", title="Backend Engineer")], "feed")
        store.upsert_postings([_row("c", title="Data Engineer")], "board")
        self.assertIsNone(self._found("a"))
        self.assertEqual(self._found("b"), store.get_posting("b")["first_seen_at"])
        self.assertIsNone(self._found("c"))

    def test_the_migration_leaves_each_sources_first_read_unfound(self):
        store.upsert_postings([_row("a"), _row("b", title="Backend Engineer")], "feed")
        with self.conn:
            self.conn.execute("UPDATE postings SET first_seen_at = '2026-09-01T00:00:00', "
                              "found_at = NULL")
            self.conn.execute("UPDATE postings SET first_seen_at = '2026-09-02T00:00:00' "
                              "WHERE id = 'b'")
            self.conn.execute("ALTER TABLE postings DROP COLUMN found_at")
            store.schema._add_found_at(self.conn)
        self.assertEqual((self._found("a"), self._found("b")), (None, "2026-09-02T00:00:00"))

    def _times(self, posting_id):
        row = self.conn.execute("SELECT first_seen_at, last_seen_at FROM postings "
                                "WHERE id = ?", (posting_id,)).fetchone()
        return tuple(row)

    def test_insert_then_update(self):
        real_now = store.db.now
        self.addCleanup(setattr, store.db, "now", real_now)
        store.db.now = lambda: "2026-09-01T00:00:00"
        self.assertEqual(store.upsert_postings([_row("a")], "feed"), ["a"])
        store.db.now = lambda: "2026-09-02T00:00:00"
        store.upsert_postings([_row("a", "Software Engineer II")], "feed")
        self.assertEqual(self._times("a"), ("2026-09-01T00:00:00", "2026-09-02T00:00:00"))
        self.assertEqual(store.get_posting("a")["title"], "Software Engineer II")
        self.assertEqual(store.counts()["postings"], 1)

    def test_the_raw_feed_shape_round_trips(self):
        store.upsert_postings([_row("a", terms=["Fall 2026"], degrees=["Bachelor's"])], "feed")
        job = store.get_posting("a")
        self.assertEqual(job["company_name"], "Acme")
        self.assertEqual(job["locations"], ["Seattle, WA"])
        self.assertEqual(job["terms"], ["Fall 2026"])
        self.assertEqual(job["degrees"], ["Bachelor's"])
        self.assertEqual(job["date_posted"], NOW)
        self.assertEqual(job["source"], "feed")
        self.assertTrue(job["active"] and job["is_visible"])

    def test_a_relative_date_is_kept_only_for_a_new_posting(self):
        store.upsert_postings([_row("a", date_posted=NOW - DAY, date_is_relative=True)], "b")
        store.upsert_postings([_row("a", date_posted=NOW, date_is_relative=True)], "b")
        self.assertEqual(store.get_posting("a")["date_posted"], NOW - DAY)
        store.upsert_postings([_row("a", date_posted=NOW - 2 * DAY)], "b")
        self.assertEqual(store.get_posting("a")["date_posted"], NOW - 2 * DAY)
        store.upsert_postings([_row("undated", date_posted=None)], "b")
        store.upsert_postings([_row("undated", date_posted=NOW, date_is_relative=True)], "b")
        self.assertEqual(store.get_posting("undated")["date_posted"], NOW)

    def test_a_posting_that_changed_id_keeps_its_first_id(self):
        store.upsert_postings([_row("a", url="https://jobs.test/x?src=one")], "one")
        stored = store.upsert_postings(
            [_row("b", url="https://JOBS.test/x/?utm=two")], "two")
        self.assertEqual(stored, ["a"])
        self.assertEqual(store.counts()["postings"], 1)
        self.assertEqual(store.get_posting("a")["source"], "two")

    def test_no_url_falls_back_to_company_and_title(self):
        store.upsert_postings([_row("a", url=None), _row("b", "Data Engineer", url=None)],
                              "one")
        stored = store.upsert_postings([_row("c", url=None)], "two")
        self.assertEqual(stored, ["a"])
        self.assertEqual(store.counts()["postings"], 2)

    def test_rows_with_nothing_to_match_on_always_insert(self):
        blank = {"url": None, "company_name": None, "title": None}
        self.assertIsNone(store.url_key(blank))
        store.upsert_postings([{**blank, "id": "a"}, {**blank, "id": "b"}], "one")
        self.assertEqual(store.counts()["postings"], 2)

    def test_rows_without_an_id_are_skipped(self):
        self.assertEqual(store.upsert_postings([{"title": "SWE"}], "feed"), [])

    def test_a_row_whose_url_is_not_http_is_not_stored(self):
        rows = [_row("js", url="javascript:alert(1)"), _row("data", url="data:text/html,x"),
                _row("ok", url="HTTPS://jobs.test/ok")]
        self.assertEqual(store.upsert_postings(rows, "feed"), ["ok"])
        self.assertIsNone(store.get_posting("js"))
        self.assertIsNone(store.get_posting("data"))

    def test_a_row_without_a_url_is_still_stored(self):
        rows = [_row("a", url=None), _row("b", "Data Engineer", url="")]
        self.assertEqual(store.upsert_postings(rows, "feed"), ["a", "b"])

    def test_url_key_keeps_the_hacker_news_item_id(self):
        first = store.url_key(_row("a", url="https://news.ycombinator.com/item?id=1"))
        second = store.url_key(_row("b", url="https://news.ycombinator.com/item?id=2"))
        self.assertNotEqual(first, second)
        self.assertEqual(first, "https://news.ycombinator.com/item?id=1")

    def test_url_key_ignores_a_url_that_is_not_http(self):
        self.assertEqual(store.url_key(_row("a", url="javascript:alert(1)")),
                         store.url_key(_row("a", url=None)))

    def test_mark_inactive_missing_only_touches_that_source(self):
        store.upsert_postings([_row("a"), _row("b")], "one")
        store.upsert_postings([_row("c")], "two")
        self.assertEqual(store.mark_inactive_missing("one", ["a"]), 1)
        active = {r[0] for r in self.conn.execute("SELECT id FROM postings WHERE active = 1")}
        self.assertEqual(active, {"a", "c"})
        self.assertEqual(store.counts()["postings"], 3)  # never deleted

    def test_a_relisted_posting_comes_back_active(self):
        store.upsert_postings([_row("a")], "one")
        store.mark_inactive_missing("one", [])
        store.upsert_postings([_row("a")], "one")
        self.assertTrue(store.get_posting("a")["active"])


class NewPostingsTest(StoreTestCase):
    def test_relevant_recent_unseen_newest_first(self):
        store.upsert_postings([
            _row("old", date_posted=NOW - 60 * DAY, date_updated=NOW - 60 * DAY),
            _row("bumped", date_posted=NOW - 60 * DAY, date_updated=NOW),
            _row("undated", date_posted=None, date_updated=NOW),
            _row("older", date_posted=NOW - DAY),
            _row("newest", date_posted=NOW),
            _row("seen"),
            _row("senior", "Senior Software Engineer"),
        ], "feed")
        store.mark_seen(["seen"])
        got = store.new_postings(settings.get(), 21)
        self.assertEqual([j["id"] for j in got], ["newest", "older", "undated"])
        self.assertEqual(got[0]["company_name"], "Acme")


class SeenTest(StoreTestCase):
    def test_mark_seen_unions_and_counts(self):
        self.assertEqual(store.mark_seen(["a", "b", None, ""]), 2)
        self.assertEqual(store.mark_seen(["b", "c"]), 3)
        self.assertEqual(store.seen_ids(), {"a", "b", "c"})
        self.assertTrue(store.is_seen("a"))
        self.assertFalse(store.is_seen("z"))


class RecordsTest(StoreTestCase):
    def setUp(self):
        super().setUp()
        store.upsert_postings([_row("a"), _row("b", company="Globex")], "feed")

    def test_scores(self):
        saved = store.save_scores([
            {"id": "a", "fit": 80, "tier": 40, "below_floor": True, "fit_reason": "role",
             "tier_reason": "company"},
            {"id": "b", "fit": None}])
        self.assertEqual(saved, 1)
        job = store.get_posting("a")
        self.assertEqual((job["fit"], job["tier"], job["below_floor"], job["fit_reason"],
                          job["tier_reason"]), (80, 40, True, "role", "company"))
        self.assertIsNone(store.get_posting("b")["fit"])

    def test_reasons_change_without_touching_the_score(self):
        run_id = store.start_run()
        store.save_scores([{"id": "a", "fit": 80, "tier": 40, "below_floor": True}], run_id)
        store.save_reasons("a", "Backend role in Go.", "Small team, no public engineering.")
        job = store.get_posting("a")
        self.assertEqual((job["fit"], job["tier"], job["run_id"], job["fit_reason"],
                          job["tier_reason"]),
                         (80, 40, run_id, "Backend role in Go.",
                          "Small team, no public engineering."))

    def test_descriptions(self):
        self.assertIsNone(store.get_description("a"))
        store.save_description("a", "the text", "html", None)
        store.set_keywords("a", "TECH: Rust")
        got = store.get_description("a")
        self.assertEqual((got["text"], got["source"], got["keywords"]),
                         ("the text", "html", "TECH: Rust"))
        self.assertEqual(store.get_posting("a")["keywords"], "TECH: Rust")

    def test_keywords_without_a_description_row(self):
        store.set_keywords("b", "TECH: Go")
        self.assertEqual(store.get_description("b")["keywords"], "TECH: Go")
        self.assertIsNone(store.get_description("b")["text"])

    def _statuses(self, posting_id):
        return [e["status"] for e in store.application_events(posting_id)]

    def test_applied_at_is_set_by_the_first_sent_status_and_then_kept(self):
        saved = store.set_status("a", "saved")
        self.assertEqual((saved["status"], saved["applied_at"], saved["company"]),
                         ("saved", None, "Acme"))
        self.assertIsNotNone(store.set_status("a", "applied")["applied_at"])
        with self.conn:
            self.conn.execute("UPDATE applications SET applied_at = '2026-01-01T00:00:00'")
        interviewing = store.set_status("a", "interviewing")
        self.assertEqual((interviewing["status"], interviewing["applied_at"]),
                         ("interviewing", "2026-01-01T00:00:00"))
        self.assertEqual(self._statuses("a"), ["saved", "applied", "interviewing"])

    def test_events_are_appended_only_when_the_status_changes(self):
        store.set_status("a", "applied", "referred")
        store.set_status("a", "applied")
        store.set_status("a", "applied", "second note")
        self.assertEqual(store.application_events("a")[0]["note"], "referred")
        self.assertEqual(self._statuses("a"), ["applied"])

    def test_note_changes_only_when_one_is_given(self):
        self.assertEqual(store.set_status("a", "saved", "referred")["note"], "referred")
        self.assertEqual(store.set_status("a", "applied")["note"], "referred")
        self.assertEqual(store.set_status("a", "applied", "")["note"], "")
        self.assertEqual(store.set_note("a", "call Kim")["note"], "call Kim")
        self.assertEqual(self._statuses("a"), ["saved", "applied"])
        self.assertIsNone(store.set_note("b", "no row"))
        self.assertIsNone(store.application("b"))

    def test_an_unknown_status_is_a_value_error(self):
        with self.assertRaisesRegex(ValueError, "saved, applied"):
            store.set_status("a", "ghosted")
        self.assertIsNone(store.application("a"))

    def test_clear_forgets_the_row_and_its_history(self):
        store.set_status("a", "saved")
        store.set_status("a", "applied")
        store.clear_application("a")
        self.assertIsNone(store.application("a"))
        self.assertEqual(store.application_events("a"), [])
        self.assertEqual(store.applications(), [])

    def test_applications_join_the_posting_and_filter_by_status(self):
        self.enterContext(mock.patch.object(store.db, "now", return_value="2026-09-01T09:00:00"))
        store.set_status("a", "applied", "referred")
        store.save_scores([{"id": "a", "fit": 80, "tier": 40, "below_floor": True}])
        store.db.now.return_value = "2026-09-02T09:00:00"
        store.set_status("b", "saved")
        self.assertEqual([r["id"] for r in store.applications()], ["b", "a"])
        self.assertEqual(store.applications(["applied"]), [{
            "id": "a", "status": "applied", "applied_at": "2026-09-01T09:00:00",
            "note": "referred", "created_at": "2026-09-01T09:00:00",
            "updated_at": "2026-09-01T09:00:00", "company": "Acme",
            "title": "Software Engineer", "url": "https://jobs.test/a",
            "locations": ["Seattle, WA"], "source": "feed", "posted_at": NOW, "fit": 80,
            "tier": 40, "below_floor": True, "logo_domain": None, "next_stage": None,
            "checklist": {"done": 0, "total": 4}}])
        self.assertEqual([r["id"] for r in store.applications(["saved", "offer"])], ["b"])
        self.assertEqual(store.applications([]), [])

    def test_resolve(self):
        store.upsert_postings([_row("abc123"), _row("abd456")], "feed")
        self.assertEqual(store.resolve("abc123"), "abc123")
        self.assertEqual(store.resolve("abc"), "abc123")
        self.assertIsNone(store.resolve("ab"))
        self.assertIsNone(store.resolve("zzz"))

    def test_runs(self):
        run_id = store.start_run("/tmp/run.log")
        self.assertEqual(store.list_runs()[0]["status"], "running")
        store.finish_run(run_id, "ok", {"new": 3})
        latest = store.list_runs(1)[0]
        self.assertEqual((latest["status"], latest["counts"], latest["log_path"]),
                         ("ok", {"new": 3}, "/tmp/run.log"))
        self.assertIsNotNone(latest["finished_at"])

    def test_a_run_left_running_is_marked_interrupted(self):
        killed = store.start_run()
        store.start_run()
        statuses = {r["id"]: r["status"] for r in store.list_runs()}
        self.assertEqual(statuses[killed], "interrupted")
        self.assertEqual(store.list_runs(1)[0]["status"], "running")

    def test_relevant_ranked_counts_the_scored_postings_the_jobs_list_keeps(self):
        store.upsert_postings([_row("senior", "Senior Software Engineer")], "feed")
        first, second = store.start_run(), store.start_run()
        store.save_scores([{"id": "a", "fit": 70}, {"id": "senior", "fit": 60}], first)
        store.save_scores([{"id": "b", "fit": 50}], second)
        self.assertEqual(store.relevant_ranked(settings.get(), [first, second, 999]),
                         {first: 1, second: 1})

    def test_counts(self):
        self.assertIsNone(store.counts()["latest_run"])
        store.mark_seen(["a", "legacy-only"])
        store.save_scores([{"id": "b", "fit": 50, "tier": 50}])
        store.set_status("a", "interviewing")
        store.set_status("b", "saved")
        store.mark_inactive_missing("feed", ["a"])
        store.finish_run(store.start_run(), "ok", {"new": 1})
        run_id = store.start_run()
        store.save_scores([{"id": "a", "fit": 70, "tier": 70}], run_id)
        counts = store.counts()
        latest = counts.pop("latest_run")
        self.assertEqual(counts, {
            "postings": 2, "active": 1, "seen": 2, "scored": 2, "applied": 1, "runs": 2,
            "schema_version": store.SCHEMA_VERSION,
            "applications": {"saved": 1, "applied": 0, "interviewing": 1, "offer": 0,
                             "rejected": 0, "withdrawn": 0, "passed": 0}})
        self.assertEqual(set(latest), {"id", "started_at", "finished_at", "status",
                                       "counts", "ranked"})
        self.assertEqual((latest["id"], latest["status"], latest["counts"], latest["ranked"]),
                         (run_id, "running", None, 1))


class StageTest(StoreTestCase):
    def setUp(self):
        super().setUp()
        store.upsert_postings([_row("a"), _row("b", company="Globex")], "feed")

    def test_a_stage_on_a_posting_without_a_status_starts_interviewing(self):
        event = store.add_stage("a", "screen", "2026-10-01T14:00", "with Kim")
        self.assertEqual({k: event[k] for k in ("posting_id", "status", "stage", "at", "note")},
                         {"posting_id": "a", "status": None, "stage": "screen",
                          "at": "2026-10-01T14:00:00", "note": "with Kim"})
        self.assertEqual(store.application("a")["status"], "interviewing")
        self.assertEqual([(e["status"], e["stage"]) for e in store.application_events("a")],
                         [("interviewing", None), (None, "screen")])

    def test_a_stage_keeps_an_existing_status(self):
        store.set_status("a", "applied")
        store.add_stage("a", "oa", "2026-10-01")
        self.assertEqual(store.application("a")["status"], "applied")

    def test_status_changes_and_stages_come_back_in_time_order(self):
        with mock.patch.object(store.db, "now", return_value="2026-09-01T09:00:00"):
            store.set_status("a", "applied")
        store.add_stage("a", "onsite", "2026-09-20T10:00")
        store.add_stage("a", "screen", "2026-09-05T10:00")
        with mock.patch.object(store.db, "now", return_value="2026-09-10T09:00:00"):
            store.set_status("a", "interviewing")
        self.assertEqual([(e["status"] or e["stage"], e["at"])
                          for e in store.application_events("a")],
                         [("applied", "2026-09-01T09:00:00"), ("screen", "2026-09-05T10:00:00"),
                          ("interviewing", "2026-09-10T09:00:00"),
                          ("onsite", "2026-09-20T10:00:00")])

    def test_a_time_with_an_offset_is_stored_as_local_time(self):
        at = datetime.datetime(2026, 10, 1, 18, 0, tzinfo=datetime.timezone.utc)
        event = store.add_stage("a", "final", at.isoformat())
        self.assertEqual(event["at"], at.astimezone().replace(tzinfo=None).isoformat())

    def test_a_bad_stage_or_time_is_a_value_error_and_writes_nothing(self):
        with self.assertRaisesRegex(ValueError, "screen, oa"):
            store.add_stage("a", "lunch", "2026-10-01")
        for at in ("next tuesday", "", None):
            with self.assertRaisesRegex(ValueError, "ISO"):
                store.add_stage("a", "screen", at)
        self.assertIsNone(store.application("a"))
        self.assertEqual(store.application_events("a"), [])

    def test_update_changes_only_what_is_given(self):
        event = store.add_stage("a", "screen", "2026-10-01T09:00", "call")
        changed = store.update_stage(event["id"], at="2026-10-02T11:30")
        self.assertEqual((changed["stage"], changed["at"], changed["note"]),
                         ("screen", "2026-10-02T11:30:00", "call"))
        changed = store.update_stage(event["id"], stage="onsite", note="")
        self.assertEqual((changed["stage"], changed["note"]), ("onsite", ""))
        with self.assertRaises(ValueError):
            store.update_stage(event["id"], stage="lunch")
        self.assertIsNone(store.update_stage(9999, note="x"))

    def test_a_status_change_is_not_a_stage(self):
        store.set_status("a", "applied")
        status_event = store.application_events("a")[0]["id"]
        self.assertIsNone(store.get_stage(status_event))
        self.assertIsNone(store.update_stage(status_event, note="x"))
        self.assertIsNone(store.delete_stage(status_event))
        self.assertEqual(len(store.application_events("a")), 1)

    def test_delete_returns_the_posting(self):
        event = store.add_stage("a", "screen", "2026-10-01")
        self.assertEqual(store.delete_stage(event["id"]), "a")
        self.assertIsNone(store.delete_stage(event["id"]))
        self.assertEqual([e["stage"] for e in store.application_events("a")], [None])

    def test_upcoming_keeps_the_next_days_soonest_first(self):
        store.add_stage("a", "onsite", _in(days=3))
        store.add_stage("b", "screen", _in(hours=2))
        store.add_stage("a", "screen", _in(days=-1))
        store.add_stage("b", "final", _in(days=9))
        upcoming = store.upcoming_stages(7)
        self.assertEqual([(r["id"], r["stage"]) for r in upcoming],
                         [("b", "screen"), ("a", "onsite")])
        self.assertEqual(set(upcoming[0]), {"event_id", "id", "company", "title", "url",
                                            "status", "stage", "at", "note", "created_at",
                                            "updated_at"})
        self.assertEqual(upcoming[0]["company"], "Globex")

    def test_past_due_is_a_passed_stage_with_nothing_after_it(self):
        with mock.patch.object(store.db, "now", return_value=_in(days=-10)):
            store.set_status("a", "interviewing")
            store.set_status("b", "interviewing")
        store.add_stage("a", "screen", _in(days=-3))
        store.add_stage("b", "screen", _in(days=-5))
        store.add_stage("b", "onsite", _in(days=2))
        self.assertEqual([(r["id"], r["stage"]) for r in store.past_due_stages()],
                         [("a", "screen")])
        store.set_status("a", "offer")
        self.assertEqual(store.past_due_stages(), [])

    def test_past_due_counts_from_the_time_given(self):
        with mock.patch.object(store.db, "now", return_value=_in(days=-10)):
            store.set_status("a", "interviewing")
        store.add_stage("a", "screen", _in(days=-3))
        before = datetime.datetime.now()
        self.assertEqual([r["id"] for r in store.past_due_stages(before)], ["a"])
        self.assertEqual(store.past_due_stages(before - datetime.timedelta(days=4)), [])

    def test_a_closed_application_has_no_stages_ahead_or_due(self):
        store.add_stage("a", "screen", _in(days=-3))
        store.add_stage("b", "onsite", _in(days=2))
        for posting_id, status in (("a", "rejected"), ("b", "withdrawn")):
            store.set_status(posting_id, status)
        self.assertEqual(store.past_due_stages(), [])
        self.assertEqual(store.upcoming_stages(7), [])
        wide = datetime.timedelta(days=30)
        now = datetime.datetime.now()
        self.assertEqual(store.stages_between(now - wide, now + wide), [])

    def test_a_stage_records_when_it_was_written_and_changed(self):
        with mock.patch.object(store.db, "now", return_value="2026-09-01T09:00:00"):
            event = store.add_stage("a", "screen", "2026-10-01T09:00")
        with mock.patch.object(store.db, "now", return_value="2026-09-02T10:00:00"):
            store.update_stage(event["id"], note="moved")
        row = store.stages_between(datetime.datetime(2026, 10, 1),
                                   datetime.datetime(2026, 10, 2))[0]
        self.assertEqual((row["created_at"], row["updated_at"]),
                         ("2026-09-01T09:00:00", "2026-09-02T10:00:00"))

    def test_an_update_that_gives_nothing_is_refused(self):
        event = store.add_stage("a", "screen", "2026-10-01T09:00")
        with self.assertRaisesRegex(ValueError, "nothing to change"):
            store.update_stage(event["id"])
        self.assertEqual(store.get_stage(event["id"]), event)

    def test_a_time_out_of_range_is_a_value_error(self):
        with self.assertRaisesRegex(ValueError, "out of range"):
            store.add_stage("a", "screen", "0001-01-01T00:00:00+05:00")
        self.assertIsNone(store.application("a"))

    def test_an_application_row_carries_its_next_stage_and_checklist(self):
        store.set_status("a", "applied")
        store.add_stage("a", "screen", _in(days=-1))
        soon = store.add_stage("a", "onsite", _in(days=2))
        store.add_stage("a", "final", _in(days=5))
        store.set_item(store.checklist("a")[0]["id"], done=True)
        row = store.application("a")
        self.assertEqual(row["next_stage"], {"stage": "onsite", "at": soon["at"]})
        self.assertEqual(row["checklist"], {"done": 1, "total": 4})
        self.assertEqual([r["next_stage"] for r in store.applications()], [row["next_stage"]])

    def test_clear_forgets_stages_checklist_and_files(self):
        store.set_status("a", "applied")
        store.add_stage("a", "screen", "2026-10-01")
        home = self.enterContext(tempfile.TemporaryDirectory())
        self.enterContext(user_home(home))
        path = Path(home) / "cv.pdf"
        path.write_text("cv", encoding="utf-8")
        store.add_attachment("a", "CV", str(path))
        store.clear_application("a")
        self.assertEqual((store.application_events("a"), store.checklist("a"),
                          store.attachments("a")), ([], [], []))


class ChecklistTest(StoreTestCase):
    def setUp(self):
        super().setUp()
        store.upsert_postings([_row("a"), _row("b", company="Globex")], "feed")

    def _labels(self, posting_id="a"):
        return [item["label"] for item in store.checklist(posting_id)]

    def test_the_first_move_to_applied_adds_the_defaults(self):
        store.set_status("a", "saved")
        self.assertEqual(store.checklist("a"), [])
        store.set_status("a", "applied")
        self.assertEqual(self._labels(), list(store.DEFAULT_CHECKLIST))
        self.assertEqual([item["position"] for item in store.checklist("a")], [0, 1, 2, 3])
        self.assertEqual(set(store.checklist("a")[0]),
                         {"id", "posting_id", "label", "done", "position"})

    def test_a_list_with_items_gets_no_defaults(self):
        store.add_item("a", "Portfolio link")
        store.set_status("a", "applied")
        self.assertEqual(self._labels(), ["Portfolio link"])

    def test_defaults_come_once(self):
        store.set_status("a", "applied")
        store.set_status("a", "interviewing")
        store.set_status("a", "applied")
        self.assertEqual(len(store.checklist("a")), 4)

    def test_items_append_update_and_delete(self):
        first = store.add_item("a", "  Portfolio link ")
        second = store.add_item("a", "Transcript")
        self.assertEqual((first["label"], first["done"], first["position"], second["position"]),
                         ("Portfolio link", False, 0, 1))
        self.assertTrue(store.set_item(first["id"], done=True)["done"])
        self.assertEqual(store.set_item(first["id"], label="Portfolio")["label"], "Portfolio")
        self.assertTrue(store.checklist_item(first["id"])["done"])
        with self.assertRaises(ValueError):
            store.set_item(first["id"], label="  ")
        with self.assertRaises(ValueError):
            store.add_item("a", "")
        self.assertIsNone(store.set_item(9999, done=True))
        self.assertEqual(store.delete_item(second["id"]), "a")
        self.assertIsNone(store.delete_item(second["id"]))
        self.assertEqual(self._labels(), ["Portfolio"])
        self.assertEqual(store.add_item("b", "Other")["position"], 0)

    def test_reorder_takes_every_id_once(self):
        ids = [store.add_item("a", label)["id"] for label in ("one", "two", "three")]
        other = store.add_item("b", "elsewhere")["id"]
        reordered = store.reorder("a", [ids[2], ids[0], ids[1]])
        self.assertEqual([item["label"] for item in reordered], ["three", "one", "two"])
        for bad in ([ids[0], ids[1]], [*ids, ids[0]], [ids[0], ids[1], other]):
            with self.assertRaisesRegex(ValueError, "every item"):
                store.reorder("a", bad)
        self.assertEqual(self._labels(), ["three", "one", "two"])


class AttachmentTest(StoreTestCase):
    def setUp(self):
        super().setUp()
        store.upsert_postings([_row("a")], "feed")
        self.fake_home = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(user_home(self.fake_home))
        self.outside = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.cv = self.fake_home / "cv.pdf"
        self.cv.write_text("cv", encoding="utf-8")

    def test_a_file_under_home_is_kept_by_its_real_path(self):
        added = store.add_attachment("a", " Resume ", str(self.cv))
        self.assertEqual((added["label"], added["path"], added["posting_id"]),
                         ("Resume", os.path.realpath(self.cv), "a"))
        self.assertEqual(store.attachments("a"), [added])
        self.assertTrue(self.cv.exists())

    def test_a_blank_label_takes_the_file_name_and_tilde_expands(self):
        self.assertEqual(store.add_attachment("a", "", "~/cv.pdf")["label"], "cv.pdf")

    @unittest.skipIf(sys.platform == "win32", "Windows needs extra rights to make a symlink")
    def test_a_symlink_is_resolved_before_the_check(self):
        target = self.outside / "secret.txt"
        target.write_text("x", encoding="utf-8")
        (self.fake_home / "link.txt").symlink_to(target)
        with self.assertRaisesRegex(ValueError, "outside your home"):
            store.add_attachment("a", "link", str(self.fake_home / "link.txt"))
        (self.fake_home / "cv-link.pdf").symlink_to(self.cv)
        added = store.add_attachment("a", "cv", str(self.fake_home / "cv-link.pdf"))
        self.assertEqual(added["path"], os.path.realpath(self.cv))

    def test_what_is_refused(self):
        outside = self.outside / "notes.txt"
        outside.write_text("x", encoding="utf-8")
        (self.fake_home / "folder").mkdir()
        for path, message in ((str(outside), "outside your home"),
                              (str(self.fake_home / "folder"), "not a file"),
                              (str(self.fake_home / "missing.pdf"), "not a file"),
                              ("cv.pdf", "not an absolute path"),
                              (str(self.fake_home / ".." / outside.name), "outside your home"),
                              ("", "needs a path")):
            with self.assertRaisesRegex(ValueError, message):
                store.add_attachment("a", "x", path)
        self.assertEqual(store.attachments("a"), [])

    def test_a_path_is_taken_as_given_and_trimmed_only_when_nothing_is_there(self):
        spaced = self.fake_home / "notes.txt "
        spaced.write_text("x", encoding="utf-8")
        self.assertEqual(store.add_attachment("a", "", str(spaced))["path"],
                         os.path.realpath(spaced))
        self.assertEqual(store.add_attachment("a", "", f"  {self.cv}\n")["path"],
                         os.path.realpath(self.cv))
        with self.assertRaisesRegex(ValueError, "not a file"):
            store.add_attachment("a", "", f"{self.fake_home / 'missing.pdf'} ")

    def test_home_is_matched_by_the_file_system_on_a_case_insensitive_volume(self):
        shouted = Path(str(self.cv).upper())
        if not shouted.exists():
            self.skipTest("the temporary folder is on a case-sensitive volume")
        added = store.add_attachment("a", "", str(shouted))
        self.assertTrue(os.path.samefile(added["path"], self.cv))

    def test_delete_keeps_the_file(self):
        added = store.add_attachment("a", "cv", str(self.cv))
        self.assertEqual(store.delete_attachment(added["id"]), "a")
        self.assertIsNone(store.delete_attachment(added["id"]))
        self.assertTrue(self.cv.exists())


class SearchTest(StoreTestCase):
    def setUp(self):
        super().setUp()
        store.upsert_postings([
            _row("jane", "Software Engineer", "Jane Street", date_posted=NOW - 3 * DAY),
            _row("citadel", "Quantitative Developer", "Citadel", category="Quant",
                 locations=["Chicago, IL"], date_posted=NOW - DAY),
            _row("acme", "Backend Engineer", "Acme", locations=["Remote"],
                 date_posted=NOW - 10 * DAY, date_updated=NOW - 10 * DAY),
            _row("senior", "Senior Software Engineer", "Globex"),
        ], "one")
        store.upsert_postings([_row("other", "Platform Engineer", "Initech")], "two")
        store.save_scores([{"id": "jane", "fit": 90, "tier": 95},
                           {"id": "citadel", "fit": 85, "tier": 90},
                           {"id": "acme", "fit": 95, "tier": 30, "below_floor": True}])
        store.save_description("citadel", "text", "html", None, "TECH: C++, kdb")
        store.set_status("jane", "applied")

    def _ids(self, **kwargs):
        rows, total = store.search(**kwargs)
        self.assertEqual(total, len(rows))
        return [r["id"] for r in rows]

    def test_default_is_relevant_active_sorted_by_score(self):
        self.assertEqual(self._ids(), ["jane", "citadel", "acme", "other"])

    def test_list_filters_and_run_id(self):
        self.assertEqual(self._ids(source=["one", "two"]), ["jane", "citadel", "acme", "other"])
        self.assertEqual(self._ids(source=[]), [])
        self.assertEqual(self._ids(category=["Quant"]), ["citadel"])
        self.assertEqual(self._ids(category="Quant"), ["citadel"])
        run_id = store.start_run("log")
        store.save_scores([{"id": "other", "fit": 70, "tier": 70}], run_id)
        self.assertEqual(self._ids(run_id=run_id), ["other"])
        rows, _ = store.search(run_id=run_id)
        self.assertEqual(rows[0]["run_id"], run_id)

    def test_rows_carry_the_company_s_latest_sent_application(self):
        store.upsert_postings([_row("jane2", "Software Engineer II", "jane street")], "one")
        rows = {r["id"]: r for r in store.search()[0]}
        applied_at = store.application("jane")["applied_at"]
        self.assertEqual(rows["jane2"]["applied_before_at"], applied_at)
        self.assertEqual(rows["jane"]["applied_before_at"], applied_at)
        self.assertIsNone(rows["citadel"]["applied_before_at"])
        store.set_status("citadel", "saved")
        self.assertIsNone(store.search()[0][1]["applied_before_at"])

    def test_relevant_only_false_includes_everything(self):
        self.assertIn("senior", self._ids(relevant_only=False))

    def test_full_text_matches_company_title_and_keywords(self):
        self.assertEqual(self._ids(q="jane"), ["jane"])
        self.assertEqual(self._ids(q="quant dev"), ["citadel"])
        self.assertEqual(self._ids(q="kdb"), ["citadel"])
        self.assertEqual(self._ids(q="   "), self._ids())

    def test_full_text_matches_terms_category_and_locations(self):
        store.upsert_postings([_row("winter", "Software Engineer Intern", "Hooli",
                                    terms=["Winter 2027"], locations=["Toronto, ON"])], "one")
        self.assertEqual(self._ids(q="Winter 2027", relevant_only=False), ["winter"])
        self.assertEqual(self._ids(q="hooli toronto", relevant_only=False), ["winter"])
        self.assertEqual(self._ids(q="quant chicago"), ["citadel"])

    def test_the_migration_indexes_what_search_reads(self):
        with self.conn:
            self.conn.execute("UPDATE postings SET terms = '[\"Fall 2026\"]' WHERE id = 'jane'")
            self.conn.execute("DROP TABLE postings_fts")
            self.conn.execute("CREATE VIRTUAL TABLE postings_fts "
                              "USING fts5(id UNINDEXED, company, title, keywords)")
            store.schema._index_search(self.conn)
        self.assertEqual(self._ids(q="fall 2026"), ["jane"])
        self.assertEqual(self._ids(q="kdb"), ["citadel"])

    def test_query_syntax_in_user_input_is_literal(self):
        for q in ('"', "AND", "c++ OR", "(title:", "NEAR(a b)", "*", "\x00", "jane\x00"):
            store.search(q=q)
        self.assertEqual(self._ids(q="ja\x00ne"), ["jane"])

    def test_location_never_matches_json_punctuation(self):
        self.assertEqual(self._ids(location='"'), [])
        self.assertEqual(self._ids(location='", "'), [])
        self.assertEqual(self._ids(location="chicago, il"), ["citadel"])

    def test_filters(self):
        self.assertEqual(self._ids(fit_min=90), ["jane", "acme"])
        self.assertEqual(self._ids(tier_min=90), ["jane", "citadel"])
        self.assertEqual(self._ids(category="Quant"), ["citadel"])
        self.assertEqual(self._ids(location="chicago"), ["citadel"])
        self.assertEqual(self._ids(source="two"), ["other"])
        self.assertNotIn("acme", self._ids(posted_within_days=7))

    def test_postings_older_than_recent_days_show_only_with_a_status(self):
        store.upsert_postings([
            _row("stale", "Software Engineer", "Hooli", date_posted=NOW - 30 * DAY,
                 date_updated=None),
            _row("kept", "Software Engineer", "Pied Piper", date_posted=NOW - 30 * DAY,
                 date_updated=None),
        ], "one")
        store.set_status("kept", "saved")
        ids = self._ids(relevant_only=False)
        self.assertNotIn("stale", ids)
        self.assertIn("kept", ids)

    def test_status_filter(self):
        store.set_status("citadel", "saved")
        self.assertEqual(self._ids(status=["applied"]), ["jane"])
        self.assertEqual(self._ids(status=["applied", "saved"]), ["jane", "citadel"])
        self.assertEqual(self._ids(status=["none"]), ["acme", "other"])
        self.assertEqual(self._ids(status="none"), ["acme", "other"])
        self.assertEqual(self._ids(status=["saved", "none"]), ["citadel", "acme", "other"])
        self.assertEqual(self._ids(status=["offer"]), [])

    def test_passed_postings_are_hidden_unless_asked_for(self):
        store.set_status("citadel", "passed")
        self.assertEqual(self._ids(), ["jane", "acme", "other"])
        self.assertEqual(self._ids(hide_passed=False), ["jane", "citadel", "acme", "other"])
        self.assertEqual(self._ids(status=["passed"]), ["citadel"])
        self.assertEqual(self._ids(status=["passed", "applied"]), ["jane", "citadel"])

    def test_rows_carry_their_status(self):
        rows, _ = store.search(q="jane")
        self.assertEqual((rows[0]["status"], rows[0]["note"]), ("applied", None))
        self.assertIsNotNone(rows[0]["applied_at"])
        self.assertEqual(rows[0]["status_updated_at"], rows[0]["applied_at"])
        self.assertIsNone(store.search(q="citadel")[0][0]["status"])

    def test_inactive_postings_are_hidden_unless_asked_for(self):
        store.mark_inactive_missing("two", [])
        self.assertNotIn("other", self._ids())
        self.assertIn("other", self._ids(relevant_only=False, active_only=False))

    def test_sorts(self):
        self.assertEqual(self._ids(sort="newest"), ["other", "citadel", "jane", "acme"])
        self.assertEqual(self._ids(sort="company"), ["acme", "citadel", "other", "jane"])
        with mock.patch.object(store.db, "now", return_value="2099-01-01T00:00:00"):
            store.set_status("acme", "saved")
        self.assertEqual(self._ids(sort="updated"), ["acme", "jane", "citadel", "other"])

    def test_pagination_reports_the_full_total(self):
        rows, total = store.search(limit=2, offset=1)
        self.assertEqual([r["id"] for r in rows], ["citadel", "acme"])
        self.assertEqual(total, 4)

    def test_rows_carry_their_joins(self):
        rows, _ = store.search(q="citadel")
        self.assertEqual((rows[0]["fit"], rows[0]["keywords"]), (85, "TECH: C++, kdb"))


class GroupTest(StoreTestCase):
    """One job listed by several sources is one row, whichever source it came from."""

    def setUp(self):
        super().setUp()
        store.upsert_postings([_row("one-swe", "Software Engineer (New Grad)", "Acme"),
                               _row("one-swe-2", "Software Engineer (New Grad)", "Acme"),
                               _row("one-sre", "SRE", "Acme")], "one")
        store.upsert_postings([_row("two-swe", "software engineer, new grad", " ACME")], "two")

    def _groups(self):
        return dict(store.connect().execute("SELECT id, group_key FROM postings"))

    def test_titles_match_without_case_punctuation_or_spacing(self):
        self.assertEqual(store.title_key("  Software Engineer, New-Grad (2026)!"),
                         "software engineer new grad 2026")
        groups = self._groups()
        self.assertEqual(groups["one-swe"], groups["two-swe"])
        self.assertNotEqual(groups["one-swe"], groups["one-swe-2"])
        self.assertNotEqual(groups["one-swe"], groups["one-sre"])

    def test_search_returns_one_row_per_role_with_the_other_sources(self):
        rows, total = store.search()
        self.assertEqual(total, 2)
        by_id = {row["id"]: row for row in rows}
        self.assertEqual(set(by_id), {"one-swe", "one-sre"})
        self.assertEqual(by_id["one-swe"]["also_on"],
                         [{"source": "two", "url": "https://jobs.test/two-swe"}])
        self.assertEqual(by_id["one-sre"]["also_on"], [])
        self.assertEqual(store.get_posting("two-swe")["also_on"],
                         [{"source": "one", "url": "https://jobs.test/one-swe"}])

    def test_a_role_open_in_several_places_is_one_row_listing_the_others(self):
        store.upsert_postings([_row("one-nyc", "Software Engineer (New Grad)", "Acme",
                                    locations=["New York, NY"])], "one")
        rows, total = store.search()
        self.assertEqual(total, 2)
        swe = next(row for row in rows if row["id"] != "one-sre")
        openings = {swe["id"]: swe["locations"]}
        openings.update((o["id"], o["locations"]) for o in swe["other_openings"])
        self.assertEqual(openings, {"one-swe": ["Seattle, WA"], "one-swe-2": ["Seattle, WA"],
                                    "one-nyc": ["New York, NY"]})
        self.assertEqual(store.facets()["source"], {"one": 2, "two": 1})

    def test_the_migration_keys_every_stored_role(self):
        with store.connect() as conn:
            conn.execute("UPDATE postings SET role_key = NULL")
            store.schema._add_role_key(conn)
        self.assertEqual(store.search()[1], 2)

    def test_a_role_listed_again_after_it_came_down_counts_its_reposts(self):
        with store.connect() as conn:
            conn.execute("UPDATE postings SET active = 0, first_seen_at = '2026-01-01T00:00:00', "
                         "last_seen_at = '2026-02-01T00:00:00' "
                         "WHERE id IN ('one-swe', 'one-swe-2', 'two-swe')")
        store.upsert_postings([_row("three-swe", "Software Engineer (New Grad)", "Acme")],
                              "three")
        by_id = {row["id"]: row for row in store.search()[0]}
        self.assertEqual((by_id["three-swe"]["reposts"], by_id["one-sre"]["reposts"]), (3, 0))
        self.assertEqual(store.get_posting("three-swe")["reposts"], 3)
        self.assertEqual(store.get_posting("one-swe")["reposts"], 0)

    def test_the_earliest_seen_member_stands_for_its_group_whatever_the_scores(self):
        self.assertIn("one-swe", [row["id"] for row in store.search()[0]])
        store.save_scores([{"id": "one-swe", "fit": 60, "tier": 60},
                           {"id": "two-swe", "fit": 90, "tier": 60}])
        ids = [row["id"] for row in store.search()[0]]
        self.assertIn("one-swe", ids)
        self.assertNotIn("two-swe", ids)
        self.assertEqual([row["id"] for row in store.search(source="two")[0]], ["two-swe"])

    def test_the_same_title_in_other_places_is_another_job(self):
        store.upsert_postings([_row("two-swe", "Software Engineer (New Grad)", "Acme",
                                    locations=["Austin, TX"])], "two")
        groups = self._groups()
        self.assertNotEqual(groups["one-swe"], groups["two-swe"])

    def test_places_match_folded_and_in_any_order_with_remote_variants_as_one(self):
        self.assertEqual(store.location_key(["Remote in USA", " new  York, NY", "NEW YORK, NY",
                                             "US - Remote"]),
                         "new york, ny|remote")
        store.upsert_postings([_row("r1", "Data Engineer", "Initech",
                                    locations=["Remote (US)", "Austin, TX"])], "one")
        store.upsert_postings([_row("r2", "Data Engineer", "Initech",
                                    locations=["austin, tx", "Remote - USA"])], "two")
        groups = self._groups()
        self.assertEqual(groups["r1"], groups["r2"])

    def test_different_requisition_ids_are_different_jobs(self):
        store.upsert_postings([
            _row("g1", "Quant Developer", "Hooli",
                 url="https://job-boards.greenhouse.io/hooli/jobs/4716932005"),
            _row("w1", "Data Analyst", "Hooli",
                 url="https://hooli.wd5.myworkdayjobs.com/Ext/job/Austin/Data-Analyst_R12345")],
            "one")
        store.upsert_postings([
            _row("g2", "Quant Developer", "Hooli",
                 url="https://hooli.com/careers?gh_jid=4716932005"),
            _row("w2", "Data Analyst", "Hooli",
                 url="https://hooli.wd5.myworkdayjobs.com/Ext/job/Austin/Data-Analyst_R12399")],
            "two")
        groups = self._groups()
        self.assertEqual(groups["g1"], groups["g2"])
        self.assertNotEqual(groups["w1"], groups["w2"])

    def test_requisition_ids_read_from_apply_urls(self):
        cases = {
            "https://boards.greenhouse.io/acme/jobs/4716932005": "4716932005",
            "https://acme.com/careers/?gh_jid=4716932005": "4716932005",
            "https://acme.com/job?jobId=A-77&src=x": "a77",
            "https://acme.com/apply?reqId=12345": "12345",
            "https://acme.wd1.myworkdayjobs.com/ext/job/NY/SWE_REQ_110256": "110256",
            "https://acme.wd1.myworkdayjobs.com/ext/job/NY/SWE_R-0012345": "0012345",
            "https://acme.icims.com/jobs/R12345/software-engineer": "12345",
            "https://acme.com/careers/job/41383/": "41383",
            "https://jobs.lever.co/acme/0b1c-22": None,
            "https://acme.com/jobs/software-engineer-2026": None,
        }
        for url, expected in cases.items():
            with self.subTest(url):
                self.assertEqual(store.requisition(url), expected)

    def test_a_status_on_one_member_is_the_group_s(self):
        store.set_status("two-swe", "applied")
        rows = {row["id"]: row for row in store.search()[0]}
        self.assertEqual(rows["two-swe"]["status"], "applied")
        self.assertNotIn("one-swe", rows)
        self.assertEqual({row["id"] for row in store.search(status="none")[0]},
                         {"one-swe-2", "one-sre"})
        store.set_status("two-swe", "passed")
        self.assertEqual({row["id"] for row in store.search()[0]}, {"one-swe-2", "one-sre"})

    def test_a_new_title_moves_a_posting_to_its_new_group(self):
        store.upsert_postings([_row("two-swe", "SRE", "Acme")], "two")
        groups = self._groups()
        self.assertEqual(groups["two-swe"], groups["one-sre"])
        self.assertNotEqual(groups["two-swe"], groups["one-swe"])

    def test_a_run_counts_a_group_once(self):
        run_id = store.start_run()
        store.save_scores([{"id": i, "fit": 80, "tier": 80}
                           for i in ("one-swe", "two-swe", "one-sre")], run_id)
        self.assertEqual(store.relevant_ranked(settings.get(), [run_id]), {run_id: 2})
        store.save_link_checks([("one-sre", "closed")])
        self.assertEqual(store.relevant_ranked(settings.get(), [run_id]), {run_id: 1})


class FeedbackTest(StoreTestCase):
    def setUp(self):
        super().setUp()
        store.upsert_postings([_row(f"p{n}", f"Engineer {n}", f"Co {n}") for n in range(8)],
                              "feed")
        store.save_scores([{"id": f"p{n}", "fit": 50 + n, "tier": 60} for n in range(8)])

    def _feedback(self, posting_id, verdict, at, reason=None):
        with mock.patch.object(store.db, "now", lambda: f"2026-09-{at:02d}T00:00:00"):
            store.set_feedback(posting_id, verdict, reason)

    def test_set_replace_clear_and_count(self):
        self.assertEqual(store.set_feedback("p0", "down", "role", "not a fit")["note"],
                         "not a fit")
        store.set_feedback("p0", "up")
        self.assertEqual({k: v for k, v in store.feedback("p0").items() if k != "created_at"},
                         {"verdict": "up", "reason": None, "note": None})
        store.set_feedback("p1", "down", "seniority")
        self.assertEqual(store.feedback_stats(), {"up": 1, "down": 1, "total": 2})
        store.clear_feedback("p0")
        self.assertIsNone(store.feedback("p0"))
        self.assertEqual(store.get_posting("p1")["feedback"],
                         {"verdict": "down", "reason": "seniority"})
        self.assertIsNone(store.get_posting("p0")["feedback"])

    def test_verdicts_and_reasons_outside_the_set_are_refused(self):
        with self.assertRaises(ValueError):
            store.set_feedback("p0", "sideways")
        with self.assertRaises(ValueError):
            store.set_feedback("p0", "up", "vibes")

    def test_examples_are_newest_first_and_balanced(self):
        for n in range(6):
            self._feedback(f"p{n}", "up", n + 1)
        self._feedback("p6", "down", 10, "role")
        self._feedback("p7", "down", 11)
        examples = store.feedback_examples(limit=4)
        self.assertEqual([(e["title"], e["verdict"]) for e in examples],
                         [("Engineer 7", "down"), ("Engineer 6", "down"),
                          ("Engineer 5", "up"), ("Engineer 4", "up")])
        self.assertEqual(examples[1]["reason"], "role")
        self.assertEqual(examples[1]["fit"], 56)
        self.assertEqual([e["verdict"] for e in store.feedback_examples(limit=6)],
                         ["down", "down", "up", "up", "up", "up"])


class SalaryAndSponsorshipTest(StoreTestCase):
    def setUp(self):
        super().setUp()
        store.upsert_postings([
            _row("yearly", company="A"), _row("hourly", company="B"),
            _row("monthly", company="C"), _row("unstated", company="D"),
            _row("capped", company="E", sponsorship="Does Not Offer Sponsorship"),
            _row("pounds", company="F"),
        ], "feed")
        usd = {"currency": "USD"}
        for posting_id, salary, sponsorship in (
                ("yearly", {"min": 120000, "max": 150000, "period": "year", **usd}, None),
                ("hourly", {"min": 45, "max": 80, "period": "hour", **usd}, False),
                ("monthly", {"min": 9000, "max": 9000, "period": "month", **usd}, None),
                ("capped", {"min": None, "max": 100000, "period": "year", **usd}, True),
                ("pounds", {"min": 900000, "max": 900000, "period": "year",
                            "currency": "GBP"}, None)):
            store.set_keywords(posting_id, "TECH: Go", salary, sponsorship)

    def _ids(self, **kwargs):
        return [row["id"] for row in store.search(**kwargs)[0]]

    def test_salary_sorts_by_the_yearly_maximum_with_unstated_last(self):
        self.assertEqual(self._ids(sort="salary"),
                         ["hourly", "yearly", "monthly", "capped", "pounds", "unstated"])
        rows = {row["id"]: row for row in store.search()[0]}
        self.assertEqual(rows["hourly"]["salary"], {"min": 45, "max": 80, "period": "hour",
                                                    "currency": "USD"})
        self.assertEqual(rows["capped"]["salary"], {"min": None, "max": 100000,
                                                    "period": "year", "currency": "USD"})
        self.assertEqual(rows["pounds"]["salary"]["currency"], "GBP")
        self.assertIsNone(rows["unstated"]["salary"])

    def test_salary_min_compares_yearly_usd_amounts(self):
        self.assertEqual(set(self._ids(salary_min=108000)), {"yearly", "hourly", "monthly"})
        self.assertEqual(set(self._ids(salary_min=160000)), {"hourly"})

    def test_the_summary_s_sponsorship_wins_over_the_feed_s(self):
        rows = {row["id"]: row["offers_sponsorship"] for row in store.search()[0]}
        self.assertEqual(rows, {"yearly": "yes", "hourly": "no", "monthly": "yes",
                                "unstated": "yes", "capped": "yes", "pounds": "yes"})
        self.assertEqual(self._ids(sponsorship="no"), ["hourly"])


class FacetTest(StoreTestCase):
    def setUp(self):
        super().setUp()
        store.upsert_postings([
            _row("a", company="A"), _row("b", company="B", category="Quant"),
            _row("c", company="C", sponsorship="Other"),
        ], "one")
        store.upsert_postings([_row("a2", company="A", url="https://x.test/a"),
                               _row("d", company="D")], "two")
        store.set_status("b", "applied")
        store.set_status("d", "passed")

    def test_each_facet_leaves_its_own_filter_out(self):
        self.assertEqual(store.facets(source="one", category="Software"), {
            "source": {"one": 2, "two": 1},
            "category": {"Software": 2, "Quant": 1},
            "status": {"none": 2},
            "sponsorship": {"yes": 1, "not stated": 1}})

    def test_status_counts_include_passed_and_groups_count_once(self):
        self.assertEqual(store.facets()["status"], {"none": 2, "applied": 1, "passed": 1})
        self.assertEqual(store.facets()["source"], {"one": 3, "two": 1})
        self.assertEqual(store.search()[1], 3)

    def test_a_count_is_the_total_that_choosing_its_value_gives(self):
        chosen = {"source": ["one"], "category": ["Software"], "sponsorship": "yes",
                  "hide_passed": True}
        facet_values = {"sponsorship": ("yes", "no")}
        for name, counts in store.facets(**chosen).items():
            for value, n in counts.items():
                if name == "status":
                    pick = {"status": [value], "hide_passed": True}
                elif name in facet_values:
                    if value not in facet_values[name]:
                        continue
                    pick = {name: value}
                else:
                    pick = {name: [value]}
                with self.subTest(facet=name, value=value):
                    self.assertEqual(store.search(**{**chosen, **pick})[1], n)


class CallCountTest(StoreTestCase):
    def test_calls_in_the_window_are_counted_by_kind(self):
        store.record_call("rank", "sonnet", 1000, 200, run_id=None)
        store.record_call("rank", "sonnet", 500, 100)
        store.record_call("summary", "haiku", 300, 50)
        with mock.patch.object(store.db, "now", lambda: "2000-01-01T00:00:00"):
            store.record_call("onboard", "sonnet", 9000, 900)
        self.assertEqual(store.call_counts(30), {"rank": 2, "summary": 1, "onboard": 0,
                                                 "total": 3, "prompt_chars": 1800})


class ImportLegacyTest(unittest.TestCase):
    def setUp(self):
        self.home = self.enterContext(temp_home())
        state = self.home / "state"
        state.mkdir()
        files = {
            "seen.json": ["a", "b", "gone"],
            "postings.json": {
                "a": {"company": "Acme", "title": "SWE", "url": "https://x.test/a",
                      "fit": 80, "tier": 70, "day": "2026-07-25",
                      "locations": ["Tampa, FL"], "category": "Software"},
                "b": {"company": "Globex", "title": "Quant", "url": "https://x.test/b",
                      "fit": 60, "tier": 20, "day": "2026-07-26"},
            },
            "descriptions.json": {
                "a": {"text": "desc", "source": "html", "error": None,
                      "keywords": "TECH: Kafka", "fetched": "2026-07-25T07:00:00"},
                "orphan": {"text": "x", "source": "html", "error": None,
                           "keywords": None, "fetched": "2026-07-25T07:00:00"},
            },
            "applications.json": {
                "a": {"company": "Acme", "title": "SWE", "url": "https://x.test/a",
                      "note": "ref", "applied_at": "2026-07-27T09:00:00"},
                "c": {"company": "Initech", "title": "SRE", "url": "https://x.test/c",
                      "note": "", "applied_at": "2026-07-28T09:00:00"},
            },
        }
        for name, data in files.items():
            (state / name).write_text(json.dumps(data), encoding="utf-8")

    def test_all_four_files_are_imported_on_first_connect(self):
        self.assertEqual(store.counts(), {
            "postings": 3, "active": 3, "seen": 3, "scored": 2, "applied": 2, "runs": 0,
            "schema_version": store.SCHEMA_VERSION, "latest_run": None,
            "applications": {"saved": 0, "applied": 2, "interviewing": 0, "offer": 0,
                             "rejected": 0, "withdrawn": 0, "passed": 0}})
        a = store.get_posting("a")
        self.assertEqual((a["source"], a["fit"], a["tier"], a["below_floor"]),
                         ("legacy", 80, 70, False))
        self.assertTrue(store.get_posting("b")["below_floor"])
        self.assertEqual(a["locations"], ["Tampa, FL"])
        self.assertEqual(a["keywords"], "TECH: Kafka")
        self.assertIsNone(store.get_description("orphan"))
        self.assertEqual(store.application("c")["company"], "Initech")
        a_row = store.application("a")
        self.assertEqual((a_row["status"], a_row["applied_at"], a_row["updated_at"]),
                         ("applied", "2026-07-27T09:00:00", "2026-07-27T09:00:00"))
        self.assertEqual(store.application_events("a"),
                         [{"id": 1, "status": "applied", "stage": None,
                           "at": "2026-07-27T09:00:00", "note": None}])
        self.assertEqual(store.search(q="kafka", relevant_only=False)[1], 1)

    def test_import_runs_once(self):
        before = store.counts()
        store.close()
        self.assertEqual(store.counts(), before)
        self.assertFalse(store.import_legacy(self.home / "state"))
        self.assertTrue((self.home / "state" / "seen.json").exists())

    def test_a_corrupt_file_is_skipped(self):
        (self.home / "state" / "seen.json").write_text("{ not json", encoding="utf-8")
        self.assertEqual(store.counts()["seen"], 0)
        self.assertEqual(store.counts()["postings"], 3)

    def _write(self, name, content):
        path = self.home / "state" / name
        path.write_bytes(content if isinstance(content, bytes) else json.dumps(content).encode())

    def _imported_flag(self):
        return store.connect().execute(
            "SELECT 1 FROM meta WHERE key = 'legacy_imported'").fetchone() is not None

    def test_invalid_utf8_seen_is_skipped_and_the_rest_imports(self):
        self._write("seen.json", b"\xff\xfe garbage")
        self.assertEqual(store.counts()["seen"], 0)
        self.assertEqual(store.counts()["postings"], 3)
        self.assertTrue(self._imported_flag())

    def test_dict_shaped_seen_is_skipped(self):
        self._write("seen.json", {"a": True})
        self.assertEqual(store.counts()["seen"], 0)
        self.assertEqual(store.counts()["applied"], 2)

    def test_bad_values_in_postings_are_coerced(self):
        self._write("postings.json", {
            "a": {"company": "Acme", "title": "SWE", "fit": "80", "tier": "x",
                  "day": 5, "locations": 5},
            "b": {"company": ["not", "text"], "title": "Quant", "fit": None}})
        a = store.get_posting("a")
        self.assertEqual((a["fit"], a["tier"], a["below_floor"], a["locations"]),
                         (80, None, None, []))
        self.assertIsNone(store.get_posting("b")["company_name"])
        self.assertEqual(store.counts()["seen"], 3)

    def test_list_keywords_in_descriptions_are_dropped(self):
        self._write("descriptions.json", {
            "a": {"text": "desc", "source": "html", "keywords": ["x"]}})
        self.assertEqual(store.get_description("a")["text"], "desc")
        self.assertIsNone(store.get_description("a")["keywords"])

    def test_a_failed_file_is_not_retried(self):
        self._write("seen.json", b"\xff")
        store.connect()
        store.close()
        self._write("seen.json", ["a"])
        self.assertEqual(store.counts()["seen"], 0)

    def test_the_connection_is_cached_only_after_the_import(self):
        real = store.legacy._import_legacy
        self.addCleanup(setattr, store.legacy, "_import_legacy", real)
        store.legacy._import_legacy = lambda conn, state_dir: 1 / 0
        with self.assertRaises(ZeroDivisionError):
            store.connect()
        store.legacy._import_legacy = real
        self.assertEqual(store.counts()["postings"], 3)

    def test_a_missing_state_dir_imports_nothing(self):
        with temp_home() as home:
            self.assertFalse(store.import_legacy(home / "state"))
            self.assertIsNone(store.connect().execute(
                "SELECT 1 FROM meta WHERE key = 'legacy_imported'").fetchone())


class CompanyTest(StoreTestCase):
    def setUp(self):
        super().setUp()
        store.upsert_postings([
            _row("old", company="Globex", url="https://globex.example/1", date_posted=NOW - 9),
            _row("new", company="Acme", url="https://acme.example/1",
                 company_url="https://simplify.jobs/c/Acme"),
            _row("same", "Backend Engineer", company="  ACME ", url="https://acme.example/2",
                 date_posted=NOW - 1),
            _row("mid", company="Initech", url="https://initech.example/1", date_posted=NOW - 5),
            _row("gone", company="Hooli", active=False),
            _row("nameless", company=None)], "feed")

    def test_each_company_of_an_active_posting_comes_once_newest_first(self):
        self.assertEqual(store.companies_without_domain(10), [
            ("acme", "Acme", "https://simplify.jobs/c/Acme",
             ["https://acme.example/1", "https://acme.example/2"]),
            ("initech", "Initech", None, ["https://initech.example/1"]),
            ("globex", "Globex", None, ["https://globex.example/1"])])
        self.assertEqual([c[0] for c in store.companies_without_domain(2)],
                         ["acme", "initech"])
        self.assertEqual(store.count_companies_without_domain(), 3)

    def test_the_company_with_the_most_active_postings_comes_first(self):
        store.upsert_postings([_row(f"globex-{i}", company="Globex", date_posted=NOW - 20)
                               for i in range(2)] +
                              [_row(f"gone-{i}", company="Initech", active=False)
                               for i in range(5)], "feed")
        self.assertEqual([c[0] for c in store.companies_without_domain(10)],
                         ["globex", "acme", "initech"])

    def test_a_company_with_a_scored_posting_comes_first_the_best_fit_and_tier_first(self):
        store.upsert_postings([_row(f"busy-{i}", company="Hooli", date_posted=NOW - 20)
                               for i in range(3)], "feed")
        store.save_scores([{"id": "old", "fit": 60, "tier": 30},
                           {"id": "mid", "fit": 90, "tier": 80}])
        self.assertEqual([c[0] for c in store.companies_without_domain(10)],
                         ["initech", "globex", "hooli", "acme"])
        for key in ("initech", "globex", "hooli", "acme"):
            store.save_company(key, f"{key}.example", "apply-url")
            store.save_logo(f"{key}.example", None, "HTTP 404")
        self.assertEqual([c[0] for c in store.companies_without_icon(10, "guess-verified")],
                         ["initech", "globex", "hooli", "acme"])
        self.assertEqual(store.company_domains(), ["initech.example", "globex.example",
                                                   "hooli.example", "acme.example"])

    def test_a_company_the_preferences_keep_comes_before_a_busier_one(self):
        store.upsert_postings([_row(f"chef-{i}", title="Pastry Chef", company="Hooli")
                               for i in range(3)], "feed")
        self.assertEqual([c[0] for c in store.companies_without_domain(10)],
                         ["acme", "initech", "globex", "hooli"])

    def test_keys_keep_only_their_companies_in_the_same_order(self):
        store.save_scores([{"id": "old", "fit": 60, "tier": 30}])
        self.assertEqual([c[0] for c in store.companies_without_domain(10, keys=[
            "acme", "globex", "nobody"])], ["globex", "acme"])
        store.save_company("acme", "acme.example", "apply-url")
        store.save_company("globex", "globex.example", "apply-url")
        store.save_logo("acme.example", "/logos/acme.example.png", source="site")
        self.assertEqual(store.company_domains(["globex"]), ["globex.example"])
        self.assertEqual(store.icon_domains(["acme", "globex", "initech"]),
                         {"acme": "acme.example"})

    def test_the_five_newest_apply_urls_are_sampled(self):
        store.upsert_postings([_row(f"more-{i}", company="Acme", date_posted=NOW - 2 - i)
                               for i in range(6)], "feed")
        urls = store.companies_without_domain(1)[0][3]
        self.assertEqual(urls, ["https://acme.example/1", "https://acme.example/2",
                                "https://jobs.test/more-0", "https://jobs.test/more-1",
                                "https://jobs.test/more-2"])

    def test_a_resolved_company_is_done_and_a_failed_one_waits_30_days(self):
        store.save_company("acme", "acme.example", "apply-url")
        store.save_company("initech", None, None, "no guess")
        self.assertEqual([c[0] for c in store.companies_without_domain(10)], ["globex"])
        self.assertEqual(store.count_companies_without_domain(), 1)
        with store.connect() as conn:
            conn.execute("UPDATE companies SET resolved_at = '2000-01-01T00:00:00'")
        self.assertEqual([c[0] for c in store.companies_without_domain(10)],
                         ["initech", "globex"])

    def test_icon_counts_cover_the_companies_of_active_postings(self):
        store.save_company("acme", "acme.example", "apply-url")
        store.save_company("initech", "initech.example", "clearbit")
        store.save_company("globex", None, None, "no guess")
        store.save_company("hooli", "hooli.example", "feed")
        store.save_logo("acme.example", "/logos/acme.example.png", source="site")
        store.save_logo("initech.example", None, "HTTP 404")
        store.save_logo("hooli.example", "/logos/hooli.example.png", source="site")
        self.assertEqual(store.icon_counts(), {"companies": 3, "resolved": 2, "with_icon": 1,
                                               "pending": 0, "failed": 2})
        with store.connect() as conn:
            conn.execute("UPDATE companies SET resolved_at = '2000-01-01T00:00:00' "
                         "WHERE name_key = 'globex'")
            conn.execute("UPDATE logos SET fetched_at = 0 WHERE domain = 'initech.example'")
        self.assertEqual(store.icon_counts(), {"companies": 3, "resolved": 2, "with_icon": 1,
                                               "pending": 1, "failed": 0})

    def test_a_company_with_no_row_is_pending(self):
        self.assertEqual(store.icon_counts(), {"companies": 3, "resolved": 0, "with_icon": 0,
                                               "pending": 3, "failed": 0})

    def test_companies_without_an_icon_are_those_with_a_later_method_and_no_recent_move(self):
        store.save_company("acme", "acme.example", "apply-url")
        store.save_company("initech", "initech.example", "guess-verified")
        store.save_company("globex", "globex.example", "feed")
        for domain in ("acme.example", "initech.example", "globex.example"):
            store.save_logo(domain, None, "HTTP 404")
        self.assertEqual(store.companies_without_icon(10, "guess-verified"), [
            ("acme", "Acme", "https://simplify.jobs/c/Acme",
             ["https://acme.example/1", "https://acme.example/2"], "acme.example", "apply-url"),
            ("globex", "Globex", None, ["https://globex.example/1"], "globex.example", "feed")])
        self.assertEqual([c[0] for c in store.companies_without_icon(10, "guess-verified",
                                                                      skip={"acme"})],
                         ["globex"])
        store.save_company("globex", "globex.example", "feed", "no icon at globex.example")
        self.assertEqual([c[0] for c in store.companies_without_icon(10, "guess-verified")],
                         ["acme"])
        with store.connect() as conn:
            conn.execute("UPDATE companies SET resolved_at = '2000-01-01T00:00:00'")
        self.assertEqual([c[0] for c in store.companies_without_icon(10, "guess-verified")],
                         ["acme", "globex"])

    def test_companies_without_a_domain_leave_out_those_skipped(self):
        self.assertEqual([c[0] for c in store.companies_without_domain(10, {"acme"})],
                         ["initech", "globex"])

    def test_clearing_failures_forgets_only_what_failed(self):
        store.save_company("acme", "acme.example", "apply-url", "no icon at acme.example")
        store.save_company("initech", None, None, "no guess")
        store.save_logo("acme.example", "/logos/acme.example.png", source="site")
        store.save_logo("initech.example", None, "HTTP 404")
        self.assertEqual(store.clear_icon_failures(), (1, 1))
        self.assertEqual(store.company_domains(), ["acme.example"])
        self.assertEqual([c[0] for c in store.companies_without_domain(10)],
                         ["initech", "globex"])
        self.assertEqual(list(store.logos()), ["acme.example"])
        self.assertIsNone(store.connect().execute(
            "SELECT error FROM companies WHERE name_key = 'acme'").fetchone()[0])

    def test_a_posting_shows_the_icon_of_its_companys_domain_once_fetched(self):
        store.save_company("acme", "acme.example", "apply-url")
        self.assertIsNone(store.get_posting("same")["logo_domain"])
        store.save_logo("acme.example", None, "404")
        self.assertIsNone(store.get_posting("same")["logo_domain"])
        store.save_logo("acme.example", "/icons/acme.example.png")
        self.assertEqual(store.get_posting("same")["logo_domain"], "acme.example")
        rows, _ = store.search(relevant_only=False, sort="newest")
        self.assertEqual({r["id"]: r["logo_domain"] for r in rows},
                         {"new": "acme.example", "same": "acme.example", "mid": None,
                          "old": None, "nameless": None})
        self.assertEqual(store.company_domains(), ["acme.example"])


class FetchTest(unittest.TestCase):
    """fetch_new stores every row of every source, and dedupes across them."""

    def setUp(self):
        self.enterContext(temp_home(sources=[
            {"kind": "github", "location": f"https://raw.githubusercontent.com/test/{name}/dev/"
                                           "listings.json"} for name in ("one", "two")]))
        self.feeds = {}
        self.enterContext(mock.patch.object(
            sources.web, "get_json",
            lambda url, timeout=None, limit=None: self._serve(url.split("/")[4])))
        self.enterContext(mock.patch.object(logos, "fetch_missing"))

    def _serve(self, repo):
        if isinstance(self.feeds[repo], Exception):
            raise self.feeds[repo]
        return self.feeds[repo]

    def test_the_whole_feed_is_stored_but_only_relevant_ones_returned(self):
        self.feeds = {"one": [_row("a"), _row("senior", "Senior Engineer")],
                      "two": [_row("b"), _row("dup", url="https://jobs.test/a")]}
        new, counts = fetch.fetch_new()
        self.assertEqual({j["id"] for j in new}, {"a", "b"})
        self.assertEqual(counts, {"total": 3, "relevant": 2, "new": 2})
        self.assertEqual(store.counts()["postings"], 3)

    def test_delisted_postings_go_inactive_and_a_down_source_does_not(self):
        self.feeds = {"one": [_row("a"), _row("b")], "two": [_row("c")]}
        fetch.fetch_new()
        self.feeds = {"one": [_row("a")], "two": RuntimeError("503")}
        fetch.fetch_new()
        self.assertEqual(
            {r[0] for r in store.connect().execute("SELECT id FROM postings WHERE active = 1")},
            {"a", "c"})

    def _legacy(self, posting_id):
        with store.connect() as conn:
            conn.execute("INSERT INTO postings (id, source, title, category, active, "
                         "visible, posted_at) VALUES (?, 'legacy', 'Software Engineer', "
                         "'Software', 1, 1, ?)", (posting_id, NOW))
        store.mark_seen([posting_id])

    def _active(self, posting_id):
        return store.get_posting(posting_id)["active"]

    def test_legacy_rows_go_inactive_when_every_source_answered(self):
        self._legacy("old")
        self.feeds = {"one": [_row("a")], "two": [_row("b")]}
        fetch.fetch_new()
        self.assertFalse(self._active("old"))

    def test_legacy_rows_stay_active_while_a_source_is_down(self):
        self._legacy("old")
        self.feeds = {"one": [_row("a")], "two": RuntimeError("503")}
        fetch.fetch_new()
        self.assertTrue(self._active("old"))

    def test_relevant_counts_only_this_fetch(self):
        self._legacy("old")
        self.feeds = {"one": [_row("a")], "two": RuntimeError("503")}
        _, counts = fetch.fetch_new()
        self.assertEqual(counts, {"total": 1, "relevant": 1, "new": 1})


class UntrustedSchemaTest(unittest.TestCase):
    def test_a_connection_distrusts_the_schema_it_opens(self):
        with temp_home():
            conn = store.connect()
            self.assertEqual(conn.execute("PRAGMA trusted_schema").fetchone()[0], 0)
            if hasattr(conn, "getconfig"):
                self.assertTrue(conn.getconfig(sqlite3.SQLITE_DBCONFIG_DEFENSIVE))
                conn.execute("PRAGMA writable_schema = ON")
                with self.assertRaises(sqlite3.OperationalError):
                    conn.execute("UPDATE sqlite_schema SET sql = sql WHERE name = 'meta'")


if __name__ == "__main__":
    unittest.main()
