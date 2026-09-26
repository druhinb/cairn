"""The years a description asks for, and the postings a limit leaves out of ranking."""
import time
import unittest
from unittest import mock

from helpers import temp_home

from cairn import store
from cairn.core import settings
from cairn.jobs import descriptions, experience


class RequiredYearsTest(unittest.TestCase):
    def test_reads_the_fewest_years_stated(self):
        cases = [
            ("5+ years of experience building APIs", 5),
            ("3-5 years of professional software experience", 3),
            ("Two or more years of industry experience", 2),
            ("8 years of experience, or 4 years of experience with a PhD", 4),
            ("A minimum of 1 year relevant work experience", 1),
            ("Build systems that last. We have 10 years in business.", None),
            ("A 4 year degree in computer science or equivalent experience", None),
            ("", None),
        ]
        for text, years in cases:
            with self.subTest(text):
                self.assertEqual(experience.required_years(text), years)


def _row(posting_id, title):
    return {"id": posting_id, "company_name": "Acme", "title": title,
            "url": f"https://jobs.test/{posting_id}", "category": "Software",
            "locations": [], "active": True, "is_visible": True, "date_posted": time.time()}


class WithinLimitTest(unittest.TestCase):
    TEXT = {"senior": "Requires 5+ years of experience. " * 20,
            "junior": "0-1 years of experience is fine. " * 20,
            "unstated": "Join a great team. " * 30,
            "intern": "Requires 5+ years of experience. " * 20}

    def setUp(self):
        self.enterContext(temp_home(max_years_required=2))
        self.enterContext(mock.patch.object(
            descriptions, "fetch_one", lambda job: (self.TEXT[job["id"]], "html", None)))
        store.upsert_postings([_row("senior", "Software Engineer"),
                               _row("junior", "Backend Engineer"),
                               _row("unstated", "Data Engineer"),
                               _row("intern", "Software Engineer Intern")], "board")

    def test_a_posting_asking_for_too_many_years_is_left_out_but_kept(self):
        new = [store.get_posting(i) for i in ("senior", "junior", "unstated", "intern")]
        kept = experience.within_limit(new)
        self.assertEqual([job["id"] for job in kept], ["junior", "unstated", "intern"])
        self.assertEqual(store.get_posting("senior")["years_required"], 5)
        where, params = store.relevant_query(settings.get())
        with store.connect() as conn:
            listed = {row[0] for row in conn.execute(f"SELECT id FROM postings WHERE {where}",
                                                     params)}
        self.assertNotIn("senior", listed)

    def test_no_limit_reads_nothing(self):
        self.enterContext(temp_home(max_years_required=None))
        new = [_row("a", "Software Engineer")]
        with mock.patch.object(descriptions, "fetch_one") as fetch_one:
            self.assertEqual(experience.within_limit(new), new)
        fetch_one.assert_not_called()


if __name__ == "__main__":
    unittest.main()
