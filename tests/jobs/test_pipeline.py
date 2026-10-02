"""pipeline.run: the events it emits, the runs row it keeps, and the run lock."""
import contextlib
import dataclasses
import datetime
import os
import subprocess
import sys
import threading
import time
import unittest
from unittest import mock

from helpers import temp_home

from cairn import store
from cairn.core import events, locks, logfile, paths, settings
from cairn.jobs import descriptions, fetch, pipeline, rank
from cairn.jobs.pipeline import RunInProgress, RunOptions

HOLD_LOCK = """
import os
import sys
from cairn.jobs import pipeline
with pipeline.run_lock():
    print("locked", os.getpid(), flush=True)
    sys.stdin.read()
"""


def setUpModule():
    # a run reads each new posting's description for its years; the fixtures' urls
    # are not real
    offline = mock.patch.object(descriptions, "fetch_one", return_value=(None, None, "offline"))
    offline.start()
    unittest.addModuleCleanup(offline.stop)


def _job(job_id):
    return {"id": job_id, "company_name": "Acme", "title": "Software Engineer, New Grad",
            "category": "Software", "locations": ["Seattle, WA"], "active": True,
            "is_visible": True, "date_posted": time.time(),
            "url": f"https://example.com/{job_id}"}


def _fake_fetch(dry_run=False, icons=True):
    jobs = [_job("a"), _job("b")]
    store.upsert_postings(jobs, "feed")
    return jobs, {"total": 2, "relevant": 2, "new": 2}


def _no_page(job):
    return None, None, "no page in tests"


def _fake_process(jobs, run_id=None):
    ranked = [{**j, "fit": 80, "tier": 70, "below_floor": False} for j in jobs]
    for j in ranked:
        events.emit("posting_ranked", id=j["id"], company=j["company_name"],
                    title=j["title"], fit=j["fit"], tier=j["tier"], below_floor=False)
    return ranked, []


def _no_link_check(ids=None, wait=0):
    return None


@contextlib.contextmanager
def _lock_held_by_child():
    """Yield the pid of a child process holding the run lock until the block exits.

    The pid comes from the child, because on Windows a venv's python.exe starts the
    real interpreter as a separate process and Popen's pid is the launcher's."""
    src = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "src")
    proc = subprocess.Popen([sys.executable, "-c", HOLD_LOCK], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, text=True,
                            env={**os.environ, "PYTHONPATH": src})
    try:
        said, _, pid = proc.stdout.readline().partition(" ")
        if said != "locked":
            raise RuntimeError("child never took the run lock")
        yield int(pid)
    finally:
        proc.stdin.close()
        proc.wait(timeout=30)
        proc.stdout.close()


class PipelineTestCase(unittest.TestCase):
    def setUp(self):
        self.home = self.enterContext(temp_home())
        for module, name, fake in ((fetch, "fetch_new", _fake_fetch),
                                   (rank, "process", _fake_process),
                                   (fetch, "check_links", _no_link_check),
                                   (fetch, "fetch_icons_later", mock.Mock),
                                   (descriptions, "fetch_one", _no_page)):
            self.addCleanup(setattr, module, name, getattr(module, name))
            setattr(module, name, fake)
        self.events = []
        self.addCleanup(events.subscribe(self.events.append))

    def kinds(self):
        return [e.kind + (f" {e.data['name']}" if e.kind.startswith("phase") else "")
                for e in self.events]


class EventSequenceTest(PipelineTestCase):
    def test_a_normal_run(self):
        result = pipeline.run(RunOptions())
        self.assertEqual(self.kinds(), [
            "run_start", "phase_start fetch", "phase_end fetch",
            "phase_start rank", "posting_ranked", "posting_ranked",
            "phase_end rank", "run_done"])
        self.assertEqual(result.status, "ok")
        self.assertEqual(self.events[-1].data["status"], "ok")
        self.assertEqual(store.seen_ids(), {"a", "b"})
        self.assertEqual(store.list_runs(1)[0]["status"], "ok")
        self.assertEqual(store.get_posting("a")["fit"], 80)
        self.assertEqual(result.counts["summarised"], 0)

    def test_a_dry_run(self):
        result = pipeline.run(RunOptions(dry_run=True))
        self.assertEqual(self.kinds(), ["run_start", "phase_start fetch", "phase_end fetch",
                                        "run_done"])
        self.assertEqual([j["id"] for j in result.results], ["a", "b"])
        self.assertEqual(store.seen_ids(), set())
        self.assertEqual(store.list_runs(1)[0]["status"], "dry-run")

    def test_a_failing_rank(self):
        def boom(jobs, run_id=None):
            raise RuntimeError("claude is down")

        rank.process = boom
        with self.assertRaisesRegex(RuntimeError, "claude is down"):
            pipeline.run(RunOptions())
        self.assertEqual(self.kinds(), ["run_start", "phase_start fetch", "phase_end fetch",
                                        "phase_start rank", "run_failed"])
        self.assertIn("claude is down", self.events[-1].data["error"])
        self.assertEqual(store.list_runs(1)[0]["status"], "failed")
        self.assertEqual(store.seen_ids(), set())

    def test_a_row_that_cannot_be_finished_fails_the_run(self):
        real_finish = store.finish_run
        self.addCleanup(setattr, store, "finish_run", real_finish)

        def finish(run_id, status, *args):
            if status != "failed":
                raise OSError("database is locked")
            real_finish(run_id, status, *args)

        store.finish_run = finish
        with self.assertRaisesRegex(OSError, "database is locked"):
            pipeline.run(RunOptions(dry_run=True))
        self.assertEqual(self.kinds()[-1], "run_failed")
        self.assertNotIn("run_done", self.kinds())
        self.assertEqual(store.list_runs(1)[0]["status"], "failed")


class FollowUpTest(PipelineTestCase):
    def test_a_run_counts_and_announces_the_applications_waiting(self):
        store.upsert_postings([{**_job("old"), "company_name": "Globex"}], "feed")
        month_ago = (datetime.datetime.now() - datetime.timedelta(days=30)).isoformat(
            timespec="seconds")
        with mock.patch.object(store.db, "now", return_value=month_ago):
            store.set_status("old", "applied")
        result = pipeline.run(RunOptions())
        self.assertEqual([row["id"] for row in result.follow_ups], ["old"])
        self.assertEqual(result.counts["follow_ups"], 1)
        self.assertIn("[follow-ups] 1 application needs a follow-up: Globex",
                      [e.data["text"] for e in self.events if e.kind == "info"])

    def test_stored_times_that_are_not_iso_are_passed_over(self):
        store.upsert_postings([{**_job("legacy"), "company_name": "Acme"},
                               {**_job("utc"), "company_name": "Globex"}], "feed")
        with store.connect() as conn:
            for posting_id, at in (("legacy", "07/27/2026"), ("utc", "2026-07-27T09:00:00Z")):
                conn.execute("INSERT INTO applications (posting_id, status, applied_at, "
                             "created_at, updated_at) VALUES (?, 'applied', ?, ?, ?)",
                             (posting_id, at, at, at))
                conn.execute("INSERT INTO application_events (posting_id, status, at) "
                             "VALUES (?, 'applied', ?)", (posting_id, at))
        result = pipeline.run(RunOptions())
        self.assertEqual((result.status, [row["id"] for row in result.follow_ups]),
                         ("ok", ["utc"]))

    def test_a_follow_up_failure_is_a_warning_and_the_run_still_succeeds(self):
        with mock.patch.object(pipeline.tracking, "stale_applications",
                               side_effect=TypeError("can't compare")):
            result = pipeline.run(RunOptions())
        self.assertEqual((result.status, result.follow_ups, result.counts["follow_ups"]),
                         ("ok", [], 0))
        warnings = [e.data["text"] for e in self.events if e.kind == "warn"]
        self.assertEqual(warnings,
                         ["[follow-ups] could not be listed: TypeError: can't compare"])
        self.assertEqual([e.data["status"] for e in self.events if e.kind == "run_done"],
                         ["ok"])
        self.assertEqual(store.list_runs(1)[0]["status"], "ok")

    def test_a_quiet_run_says_nothing_about_follow_ups(self):
        result = pipeline.run(RunOptions())
        self.assertEqual((result.follow_ups, result.counts["follow_ups"]), ([], 0))
        self.assertNotIn("info", self.kinds())


class GroupScoreTest(PipelineTestCase):
    def test_every_member_of_a_ranked_group_is_scored_and_seen(self):
        def fetch_one_job_twice(dry_run=False, icons=True):
            store.upsert_postings([_job("a")], "feed")
            store.upsert_postings([{**_job("a2"), "url": "https://other.test/a2"}], "board")
            jobs = store.new_postings(settings.get(), 21)
            return jobs, {"total": 2, "relevant": 2, "new": 2}

        def one_per_group(jobs, run_id=None):
            ranked, unranked = [], []
            groups = {}
            for job in jobs:
                groups.setdefault(job["group_key"], []).append(job["id"])
            for members in groups.values():
                job = next(j for j in jobs if j["id"] == members[0])
                ranked.append({**job, "fit": 80, "tier": 70, "below_floor": False,
                               "also_ids": members[1:]})
            return ranked, unranked

        fetch.fetch_new, rank.process = fetch_one_job_twice, one_per_group
        result = pipeline.run(RunOptions())
        self.assertEqual(result.counts["ranked"], 1)
        self.assertEqual(store.seen_ids(), {"a", "a2"})
        self.assertEqual((store.get_posting("a2")["fit"], store.get_posting("a2")["run_id"]),
                         (80, result.run_id))


class CheckTest(PipelineTestCase):
    def setUp(self):
        super().setUp()
        self.listed = [_job("w0")]
        self.enterContext(mock.patch.object(
            fetch.sources, "fetch_all",
            lambda specs: [(spec.name, list(self.listed)) for spec in specs]))
        settings.use(dataclasses.replace(settings.get(), watchlist=[
            {"kind": "greenhouse", "location": "acme", "company": "Acme", "enabled": True}]))
        pipeline.check()
        self.listed.append(_job("w1"))

    def test_a_board_read_for_the_first_time_adds_nothing(self):
        self.assertEqual(store.seen_ids(), set())
        self.assertEqual(store.get_posting("w0")["active"], True)

    def test_ranks_and_commits_what_appeared_on_the_boards(self):
        store.upsert_postings([_job("feed1")], "feed")
        results, counts = pipeline.check()
        self.assertEqual([r["id"] for r in results], ["w1"])
        self.assertEqual(counts, {"fetched": 2, "new": 1, "ranked": 1})
        self.assertEqual(store.seen_ids(), {"w1"})
        self.assertIsNone(store.get_posting("w1")["run_id"])
        self.assertEqual(store.list_runs(), [])

    def test_a_second_check_ranks_nothing(self):
        pipeline.check()
        self.assertEqual(pipeline.check()[1], {"fetched": 2, "new": 0, "ranked": 0})

    def test_the_next_run_lists_what_a_check_found(self):
        pipeline.run(RunOptions())
        pipeline.check()
        later = pipeline.run(RunOptions()).run_id
        self.assertEqual(store.get_posting("w1")["run_id"], later)

    def test_scores_older_than_the_last_run_stay_without_one(self):
        store.upsert_postings([_job("old")], "feed")
        store.save_scores([{"id": "old", "fit": 50, "tier": 50}])
        with store.connect() as conn:
            conn.execute("UPDATE scores SET scored_at = '2020-01-01T00:00:00'")
        pipeline.run(RunOptions())
        pipeline.run(RunOptions())
        self.assertIsNone(store.get_posting("old")["run_id"])

    def test_refused_while_a_run_holds_the_lock(self):
        with pipeline.run_lock(), self.assertRaises(RunInProgress):
            pipeline.check()


class ClosedLinkTest(PipelineTestCase):
    def setUp(self):
        super().setUp()
        self.asked = []

        def check_links(ids=None, wait=0):
            self.asked.append((list(ids), wait))
            store.save_link_checks([("a", "closed"), ("b", "open")])
            return {"checked": 2, "closed": 1, "closed_ids": ["a"]}
        fetch.check_links = check_links

    def test_a_posting_whose_link_closed_is_neither_ranked_nor_pushed(self):
        result = pipeline.run(RunOptions())
        self.assertEqual(self.asked, [(["a", "b"], pipeline.LINK_WAIT_SECONDS)])
        self.assertEqual([r["id"] for r in result.results], ["b"])
        self.assertEqual(store.seen_ids(), {"b"})
        self.assertIn("[links] checked 2 new, 1 closed and left out of ranking",
                      [e.data.get("text") for e in self.events])

    def test_a_dry_run_lists_only_the_open_ones(self):
        result = pipeline.run(RunOptions(dry_run=True))
        self.assertEqual([j["id"] for j in result.results], ["b"])

    def test_only_the_postings_left_by_limit_are_checked(self):
        pipeline.run(RunOptions(limit=1, dry_run=True))
        self.assertEqual(self.asked[0][0], ["a"])


class FirstRunTest(PipelineTestCase):
    """A first run ranks every new posting; the per-run bounds apply to the runs after it."""

    def setUp(self):
        super().setUp()
        now = time.time()
        # newest first, as store.new_postings orders them
        self.jobs = [{**_job(f"new{i}"), "date_posted": now - i * 60} for i in range(4)]
        self.jobs.append({**_job("old"), "date_posted": now - 18 * 86400})
        self.ranked = []
        self.summaries = []

        def fetch_all(dry_run=False, icons=True):
            store.upsert_postings(self.jobs, "feed")
            return list(self.jobs), {"total": 5, "relevant": 5, "new": 5}

        def process(jobs, run_id=None):
            self.ranked.append([job["id"] for job in jobs])
            self.summaries.append(settings.get().max_summaries_per_run)
            return _fake_process(jobs, run_id)

        fetch.fetch_new, rank.process = fetch_all, process

    def use(self, **overrides):
        settings.use(dataclasses.replace(settings.get(), **overrides))

    def test_it_ranks_every_new_posting_and_marks_them_seen(self):
        result = pipeline.run(RunOptions(first_run=True))
        self.assertEqual(self.ranked, [["new0", "new1", "new2", "new3", "old"]])
        self.assertEqual(store.seen_ids(), {"new0", "new1", "new2", "new3", "old"})
        self.assertEqual(result.counts["ranked"], 5)

    def test_max_rank_per_run_bounds_only_the_runs_after_it(self):
        self.use(max_rank_per_run=1)
        caps = []
        rank.process = lambda jobs, run_id=None: (
            caps.append(settings.get().max_rank_per_run) or _fake_process(jobs, run_id))
        pipeline.run(RunOptions(first_run=True))
        pipeline.run(RunOptions())
        self.assertEqual(caps, [None, 1])

    def test_it_summarises_at_most_ten_and_the_settings_keep_their_own(self):
        pipeline.run(RunOptions(first_run=True))
        self.assertEqual(self.summaries, [pipeline.FIRST_RUN_SUMMARIES])
        self.assertEqual(settings.get().max_summaries_per_run, 25)
        self.use(max_summaries_per_run=3)
        pipeline.run(RunOptions(first_run=True))
        self.assertEqual(self.summaries[-1], 3)

    def test_a_failed_first_run_marks_nothing_seen(self):
        def boom(jobs, run_id=None):
            raise RuntimeError("claude is down")

        rank.process = boom
        with self.assertRaisesRegex(RuntimeError, "claude is down"):
            pipeline.run(RunOptions(first_run=True))
        self.assertEqual(store.seen_ids(), set())

    def test_a_daily_run_ranks_every_new_posting(self):
        pipeline.run(RunOptions())
        self.assertEqual(self.ranked, [["new0", "new1", "new2", "new3", "old"]])


class ExperienceTest(PipelineTestCase):
    def test_years_are_read_while_ranking_and_too_many_leave_the_results(self):
        ranking = threading.Event()

        def page(job):
            self.assertTrue(ranking.wait(5))
            return ("Requires 5+ years of experience." if job["id"] == "a" else "New grads"), \
                "html", None

        def process(jobs, run_id=None):
            ranking.set()
            return _fake_process(jobs, run_id)

        self.enterContext(mock.patch.object(descriptions, "fetch_one", page))
        self.enterContext(mock.patch.object(rank, "process", process))
        result = pipeline.run(RunOptions())
        self.assertEqual([job["id"] for job in result.results], ["b"])
        self.assertEqual(result.counts["ranked"], 1)
        self.assertEqual(store.get_posting("a")["years_required"], 5)


class IconOrderTest(unittest.TestCase):
    """A run looks up company icons while it ranks, and ends once the lookup does."""

    def setUp(self):
        self.enterContext(temp_home())
        self.icons_started, self.ranking = threading.Event(), threading.Event()
        self.icons_done = threading.Event()
        jobs = [_job("a"), _job("b")]

        def fetch_missing(*args, **kwargs):
            self.icons_started.set()
            self.overlapped = self.ranking.wait(5)
            self.icons_done.set()

        def process(jobs, run_id=None):
            self.ranking.set()
            self.assertTrue(self.icons_started.wait(5))
            return _fake_process(jobs, run_id)

        self.enterContext(mock.patch.object(fetch, "_load_all", lambda: [("feed", jobs)]))
        self.enterContext(mock.patch.object(fetch, "check_links", _no_link_check))
        self.enterContext(mock.patch.object(descriptions, "fetch_one", _no_page))
        self.enterContext(mock.patch.object(fetch.logos, "fetch_missing", fetch_missing))
        self.enterContext(mock.patch.object(rank, "process", process))

    def test_a_first_run_looks_up_icons_while_it_ranks(self):
        self.assert_side_by_side(RunOptions(first_run=True))

    def test_a_daily_run_looks_up_icons_while_it_ranks(self):
        self.assert_side_by_side(RunOptions())

    def assert_side_by_side(self, opts):
        result = pipeline.run(opts)
        self.assertEqual(result.counts["ranked"], 2)
        self.assertTrue(self.icons_done.is_set() and self.overlapped)
        self.assertFalse(fetch.logos.icons_running())

    def test_a_dry_run_looks_up_no_icons(self):
        pipeline.run(RunOptions(dry_run=True))
        self.assertFalse(self.icons_started.is_set())


class LockTest(PipelineTestCase):
    def test_a_lock_held_by_another_process_refuses_the_run(self):
        with _lock_held_by_child() as pid:
            with self.assertRaises(RunInProgress) as caught:
                pipeline.run(RunOptions(dry_run=True))
        self.assertEqual(caught.exception.pid, pid)
        self.assertEqual(self.events, [])
        self.assertEqual(store.list_runs(), [])

    def test_the_lock_frees_itself_when_its_holder_exits(self):
        with _lock_held_by_child():
            pass
        self.assertTrue(paths.run_lock().exists())
        self.assertEqual(pipeline.run(RunOptions(dry_run=True)).status, "dry-run")
        self.assertEqual(paths.run_lock().read_text(encoding="utf-8"), str(os.getpid()))

    def test_a_summary_runs_while_a_run_holds_the_lock(self):
        store.upsert_postings([_job("a")], "feed")
        self.addCleanup(setattr, descriptions, "keywords", descriptions.keywords)
        descriptions.keywords = lambda job: "TECH: Go"
        with _lock_held_by_child():
            self.assertEqual(pipeline.summarise_posting("a"), "TECH: Go")
        self.assertEqual([(e.kind, e.data.get("keywords")) for e in self.events],
                         [("summary_start", None), ("summary_done", "TECH: Go")])

    def test_a_forced_summary_asks_for_a_fresh_one(self):
        store.upsert_postings([_job("a")], "feed")
        self.addCleanup(setattr, descriptions, "keywords", descriptions.keywords)
        calls = []
        descriptions.keywords = lambda job, force=False: calls.append(force) or "TECH: Go"
        pipeline.summarise_posting("a", force=True)
        pipeline.summarise_posting("a")
        self.assertEqual(calls, [True, False])

    def test_a_summary_of_an_unknown_posting(self):
        with self.assertRaises(pipeline.UnknownPosting):
            pipeline.summarise_posting("nope")
        self.assertEqual(self.events, [])

    def test_only_one_of_two_contenders_wins(self):
        start, release = threading.Barrier(2), threading.Event()
        outcomes = []

        def contend():
            start.wait()
            try:
                with pipeline.run_lock():
                    outcomes.append("won")
                    release.wait(timeout=10)
            except RunInProgress:
                outcomes.append("refused")
                release.set()

        threads = [threading.Thread(target=contend) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        self.assertEqual(sorted(outcomes), ["refused", "won"])

    def test_garbage_in_the_lock_file(self):
        paths.run_lock().write_text("not a pid", encoding="utf-8")
        self.assertEqual(pipeline.run(RunOptions(dry_run=True)).status, "dry-run")

        fd = locks.open_file(paths.run_lock(), os.O_RDWR)
        self.addCleanup(os.close, fd)
        self.assertTrue(locks.acquire(fd))
        os.ftruncate(fd, 0)
        os.write(fd, b"not a pid")
        with self.assertRaises(RunInProgress) as caught:
            pipeline.run(RunOptions(dry_run=True))
        self.assertIsNone(caught.exception.pid)


class RunLogTest(PipelineTestCase):
    def test_the_runs_row_slices_the_log_to_this_run(self):
        paths.run_log().write_text("an earlier line\n", encoding="utf-8", newline="\n")
        self.addCleanup(logfile.attach(paths.run_log()))
        first = pipeline.run(RunOptions(dry_run=True)).run_id
        pipeline.run(RunOptions(dry_run=True))

        row = next(r for r in store.list_runs() if r["id"] == first)
        self.assertEqual(row["log_path"], str(paths.run_log()))
        with open(row["log_path"], "rb") as f:
            f.seek(row["log_start"])
            lines = f.read(row["log_end"] - row["log_start"]).decode().splitlines()
        self.assertIn(f"run {first} started", lines[0])
        self.assertIn("== fetch ==", lines[1])
        self.assertIn(f"run {first} dry-run", lines[-1])
        self.assertEqual(len(lines), 4)
        self.assertNotIn(b"\x1b", paths.run_log().read_bytes())
        self.assertNotIn(b"\r", paths.run_log().read_bytes())

    def _logged_lines(self, run_id):
        row = next(r for r in store.list_runs() if r["id"] == run_id)
        with open(row["log_path"], "rb") as f:
            f.seek(row["log_start"])
            return f.read(row["log_end"] - row["log_start"]).decode().splitlines()

    def test_a_failed_run_range_ends_with_its_run_failed_line(self):
        self.addCleanup(logfile.attach(paths.run_log()))

        def boom(jobs, run_id=None):
            raise RuntimeError("claude is down")

        rank.process = boom
        with self.assertRaises(RuntimeError):
            pipeline.run(RunOptions())
        run_id = store.list_runs(1)[0]["id"]
        self.assertIn(f"run {run_id} failed: RuntimeError: claude is down",
                      self._logged_lines(run_id)[-1])

    def test_rank_and_summary_progress_are_logged_as_lines_the_run_screen_reads(self):
        """A page opened mid-run rebuilds its counts from these lines."""
        self.assertEqual(logfile.line(events.Event("rank_progress", 0, {"done": 25, "total": 60}))
                         .split("  ", 1)[1], "[rank] ranked 25 of 60")
        self.assertEqual(logfile.line(events.Event("summary_progress", 0, {"done": 3, "total": 10}))
                         .split("  ", 1)[1], "[jd] summarised 3 of 10")

    def test_an_unrecordable_log_end_is_a_warning(self):
        real = store.set_log_end
        self.addCleanup(setattr, store, "set_log_end", real)

        def locked(run_id, log_end):
            raise OSError("database is locked")

        store.set_log_end = locked
        self.assertEqual(pipeline.run(RunOptions(dry_run=True)).status, "dry-run")
        self.assertEqual(self.kinds()[-2:], ["run_done", "warn"])
        self.assertEqual(store.list_runs(1)[0]["status"], "dry-run")


if __name__ == "__main__":
    unittest.main()
