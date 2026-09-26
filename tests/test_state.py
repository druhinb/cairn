"""Postings are marked seen at exactly one point: after their scores are stored."""
import time
import unittest
from unittest import mock

from helpers import temp_home

from cairn import store
from cairn.core import paths, settings
from cairn.jobs import fetch, logos, pipeline, rank
from cairn.jobs.pipeline import RunOptions


def _job(job_id, company="Acme"):
    return {"id": job_id, "company_name": company, "title": "Software Engineer, New Grad",
            "category": "Software", "locations": ["Seattle, WA"], "active": True,
            "is_visible": True, "date_posted": time.time(),
            "url": f"https://example.com/{job_id}"}


class StateTest(unittest.TestCase):
    def setUp(self):
        self.home = self.enterContext(temp_home())
        self.enterContext(mock.patch.object(logos, "fetch_missing"))
        self.enterContext(mock.patch.object(fetch, "check_links", return_value=None))
        self._saved = {"load_all": fetch._load_all, "process": rank.process,
                       "save_scores": store.save_scores}

    def tearDown(self):
        fetch._load_all = self._saved["load_all"]
        rank.process = self._saved["process"]
        store.save_scores = self._saved["save_scores"]

    def _serve(self, jobs):
        fetch._load_all = lambda: [("feed", list(jobs))]

    def _last_run_status(self):
        return store.list_runs(1)[0]["status"]

    def test_fetch_new_never_writes_seen(self):
        store.mark_seen({"old"})
        self._serve([_job("a"), _job("b")])
        new, counts = fetch.fetch_new()
        self.assertEqual({j["id"] for j in new}, {"a", "b"})
        self.assertEqual(counts, {"total": 2, "relevant": 2, "new": 2})
        self.assertEqual(store.seen_ids(), {"old"})

    def test_seen_postings_are_not_new_again(self):
        store.mark_seen(["a"])
        self._serve([_job("a"), _job("b")])
        new, counts = fetch.fetch_new()
        self.assertEqual([j["id"] for j in new], ["b"])
        self.assertEqual(counts, {"total": 2, "relevant": 2, "new": 1})

    def test_seed_marks_every_relevant_posting(self):
        self._serve([_job("a"), _job("b"), {"id": "c", "title": "Senior Engineer",
                                            "active": True, "is_visible": True,
                                            "date_posted": time.time()}])
        self.assertEqual(fetch.seed(), 2)
        self.assertEqual(store.seen_ids(), {"a", "b"})

    def test_a_score_save_failure_leaves_seen_untouched(self):
        store.mark_seen({"old"})
        self._serve([_job("a"), _job("b")])
        rank.process = lambda jobs, run_id=None: (list(jobs), [])

        def boom(results, run_id):
            raise RuntimeError("disk full")

        store.save_scores = boom
        with self.assertRaises(RuntimeError):
            pipeline.run(RunOptions())
        self.assertEqual(store.seen_ids(), {"old"})
        self.assertEqual(self._last_run_status(), "failed")

    def test_a_failure_to_record_the_run_never_hides_the_error(self):
        self._serve([_job("a")])
        rank.process = lambda jobs, run_id=None: (list(jobs), [])

        def boom(results, run_id):
            raise RuntimeError("disk full")

        def unrecordable(run_id, status, *args):
            raise OSError("database is locked")

        store.save_scores = boom
        real_finish = store.finish_run
        self.addCleanup(setattr, store, "finish_run", real_finish)
        store.finish_run = unrecordable
        with self.assertRaisesRegex(RuntimeError, "disk full"):
            pipeline.run(RunOptions())

    def test_only_ranked_ids_are_marked_seen(self):
        self._serve([_job("a"), _job("b"), _job("c")])
        # 'c' is dropped by ranking — a failed rank batch, say.
        rank.process = lambda jobs, run_id=None: ([j for j in jobs if j["id"] != "c"], ["c"])
        self.assertEqual(pipeline.run(RunOptions()).status, "ok")
        self.assertEqual(store.seen_ids(), {"a", "b"})
        self.assertEqual(self._last_run_status(), "ok")

    def test_dry_run_marks_nothing_seen(self):
        self._serve([_job("a"), _job("b")])
        rank.process = lambda *a, **k: self.fail("dry run must not rank")
        self.assertEqual(pipeline.run(RunOptions(dry_run=True)).status, "dry-run")
        self.assertEqual(store.seen_ids(), set())
        self.assertEqual(store.counts()["postings"], 2)
        self.assertEqual(self._last_run_status(), "dry-run")

    def test_limit_holds_postings_back_unseen(self):
        self._serve([_job("a"), _job("b"), _job("c")])
        seen_by_process = []

        def fake_process(jobs, run_id=None):
            seen_by_process.extend(j["id"] for j in jobs)
            return list(jobs), []

        rank.process = fake_process
        pipeline.run(RunOptions(limit=1))
        self.assertEqual(len(seen_by_process), 1)
        self.assertEqual(len(store.seen_ids()), 1)

    def test_fit_flag_applies_to_this_run_only(self):
        self._serve([_job("a")])
        thresholds = []

        def fake_process(jobs, run_id=None):
            thresholds.append(settings.get().fit_threshold)
            return list(jobs), []

        rank.process = fake_process
        pipeline.run(RunOptions(fit=90))
        self.assertEqual(thresholds, [90])
        self.assertEqual(settings.get().fit_threshold, settings.defaults().fit_threshold)
        self.assertFalse(paths.config_file().exists())  # nothing written to disk


if __name__ == "__main__":
    unittest.main()
