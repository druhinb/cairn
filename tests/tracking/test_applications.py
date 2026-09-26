"""Application tracking: only postings in the store can be logged."""
import unittest

from helpers import temp_home

from cairn import applications, cli, store


def _posting(job_id, company="Acme"):
    return {"id": job_id, "company_name": company, "title": "SWE, New Grad",
            "url": f"https://example.com/{job_id}", "active": True, "is_visible": True}


class ApplicationsTest(unittest.TestCase):
    def setUp(self):
        self.enterContext(temp_home())

    def test_nothing_logged_reads_as_empty(self):
        self.assertEqual(store.applications(), [])
        self.assertEqual(applications.stats(), (0, 0, []))

    def test_record_resolves_metadata_from_the_stored_posting(self):
        store.upsert_postings([_posting("a")], "feed")
        entry = applications.record("a", note="referred by Kim")
        self.assertEqual((entry["company"], entry["status"]), ("Acme", "applied"))
        self.assertEqual(entry["note"], "referred by Kim")
        self.assertEqual([a["id"] for a in store.applications()], ["a"])

    def test_recording_starts_the_default_checklist(self):
        store.upsert_postings([_posting("a")], "feed")
        applications.record("a")
        self.assertEqual([item["label"] for item in store.checklist("a")],
                         list(store.DEFAULT_CHECKLIST))
        row = store.applications()[0]
        self.assertEqual((row["checklist"], row["next_stage"]), ({"done": 0, "total": 4}, None))

    def test_recording_again_keeps_the_note(self):
        store.upsert_postings([_posting("a")], "feed")
        applications.record("a", note="referred by Kim")
        self.assertEqual(applications.record("a")["note"], "referred by Kim")

    def test_an_unambiguous_prefix_is_enough(self):
        store.upsert_postings([_posting("abcdef123")], "feed")
        self.assertEqual(applications.record("abcdef")["company"], "Acme")

    def test_unknown_id_writes_nothing_and_exits_non_zero(self):
        self.assertIsNone(applications.record("not-a-real-id"))
        args = cli._parser().parse_args(["applied", "not-a-real-id"])
        self.assertEqual(cli.cmd_applied(args), 1)
        self.assertEqual(store.applications(), [])

    def test_track_sets_and_clears_a_status(self):
        store.upsert_postings([_posting("abcdef123")], "feed")

        def track(*argv):
            return cli.cmd_track(cli._parser().parse_args(["track", *argv]))

        self.assertEqual(track("abcdef", "offer", "--note", "verbal"), 0)
        self.assertEqual((store.application("abcdef123")["status"],
                          store.application("abcdef123")["note"]), ("offer", "verbal"))
        self.assertEqual(track("abcdef", "--note", "recruiter call Friday"), 0)
        self.assertEqual(store.application("abcdef123")["note"], "recruiter call Friday")
        self.assertEqual(store.application("abcdef123")["status"], "offer")
        self.assertEqual(track("abcdef", "--clear"), 0)
        self.assertIsNone(store.application("abcdef123"))
        self.assertEqual(track("abcdef", "--note", "orphan note"), 2)
        self.assertEqual(track("abcdef"), 2)
        self.assertEqual(track("abcdef", "saved", "--clear"), 2)
        self.assertEqual(track("abcdef", "--clear", "--note", "x"), 2)
        self.assertEqual(track("nope", "saved"), 1)

    def test_annotate_flags_a_company_applied_to_before(self):
        store.upsert_postings([_posting("a", "Acme")], "feed")
        applications.record("a")
        store.set_status("a", "interviewing")
        results = [_posting("b", "ACME"), _posting("c", "Globex")]
        applications.annotate(results)
        self.assertTrue(results[0]["repeat"])
        before = results[0]["applied_before"]
        self.assertEqual((before["company"], before["status"]), ("Acme", "interviewing"))
        self.assertEqual(len(before["date"]), 10)
        self.assertNotIn("repeat", results[1])

    def test_a_saved_posting_is_not_applied_before(self):
        store.upsert_postings([_posting("a", "Acme"), _posting("p", "Initech")], "feed")
        store.set_status("a", "saved")
        store.set_status("p", "passed")
        results = [_posting("a", "Acme"), _posting("b", "Acme"), _posting("q", "Initech")]
        applications.annotate(results)
        for r in results:
            self.assertNotIn("repeat", r)
            self.assertNotIn("applied_before", r)

    def test_stats_counts_sent_applications_by_applied_at(self):
        store.upsert_postings([_posting("a"), _posting("b", "Globex"),
                               _posting("c", "Initech"), _posting("d", "Hooli")], "feed")
        applications.record("a")
        applications.record("b")
        store.set_status("b", "rejected")
        store.set_status("c", "saved")
        store.set_status("d", "applied")
        with store.connect() as conn:
            conn.execute("UPDATE applications SET applied_at = '2020-01-01T00:00:00' "
                         "WHERE posting_id = 'd'")
        total, this_week, recent = applications.stats()
        self.assertEqual((total, this_week), (3, 2))
        self.assertEqual([job_id for job_id, _ in recent][-1], "d")
        self.assertEqual({job_id for job_id, _ in recent}, {"a", "b", "d"})


if __name__ == "__main__":
    unittest.main()
