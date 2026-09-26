"""Feeds spell one city several ways: SimplifyJobs writes "NYC", a company board
"New York, NY". Every spelling of a city must match the same preference and group
as one job."""
import time
import unittest

from helpers import temp_home

from cairn import store
from cairn.jobs import fetch, places

NOW = time.time()


def _row(posting_id, place, company="Acme", title="Software Engineer"):
    return {"id": posting_id, "company_name": company, "title": title,
            "url": f"https://jobs.test/{posting_id}", "locations": [place],
            "category": "Software", "terms": [], "degrees": [], "active": True,
            "is_visible": True, "date_posted": NOW, "date_updated": NOW}


class CanonicalTest(unittest.TestCase):
    def test_each_spelling_of_a_city_is_written_one_way(self):
        cases = {
            "NYC": "New York, NY", "New York City, NY": "New York, NY",
            "new york": "New York, NY", "New York, New York": "New York, NY",
            "SF": "San Francisco, CA", "San Francisco, California": "San Francisco, CA",
            "San Francisco, CA, US": "San Francisco, CA", "South SF": "South San Francisco, CA",
            "LA": "Los Angeles, CA", "Washington, D.C.": "Washington, DC",
            "Seattle": "Seattle, WA", "Seattle, Washington": "Seattle, WA",
            "AUSTIN, Texas": "AUSTIN, TX", "  Boston,   MA ": "Boston, MA",
        }
        for given, expected in cases.items():
            with self.subTest(given=given):
                self.assertEqual(places.canonical(given), expected)

    def test_a_state_code_after_a_city_is_left_alone(self):
        for place in ("Baton Rouge, LA", "Toronto, ON, Canada", "London, UK",
                      "Remote in USA", "Washington", "San Francisco, CA +1"):
            with self.subTest(place=place):
                self.assertEqual(places.canonical(place), place)


class LocationPreferenceTest(unittest.TestCase):
    def _kept(self, allow, rows):
        with temp_home(location_allow=allow):
            store.upsert_postings(rows, "feed")
            stored = {r["id"] for r in store.connect().execute(
                "SELECT id FROM postings WHERE relevant = 1")}
            fetched = {row["id"] for row in rows if fetch._relevant(row)}
        self.assertEqual(stored, fetched)
        return stored

    def test_any_spelling_of_a_city_keeps_every_other_spelling(self):
        rows = [_row("abbrev", "NYC"), _row("full", "New York, NY"),
                _row("city", "New York City, NY"), _row("newark", "Newark, NJ")]
        for allow in (["NYC"], ["New York"], ["new york, ny"]):
            with self.subTest(allow=allow):
                self.assertEqual(self._kept(allow, rows), {"abbrev", "full", "city"})

    def test_la_means_los_angeles_and_not_louisiana(self):
        rows = [_row("la", "LA"), _row("full", "Los Angeles, CA"),
                _row("louisiana", "Baton Rouge, LA"), _row("dallas", "Dallas, TX")]
        self.assertEqual(self._kept(["LA"], rows), {"la", "full"})


class StoredPlaceTest(unittest.TestCase):
    def setUp(self):
        self.enterContext(temp_home())

    def test_a_posting_is_stored_and_searched_under_the_one_spelling(self):
        store.upsert_postings([_row("sf", "SF")], "feed")
        self.assertEqual(store.get_posting("sf")["locations"], ["San Francisco, CA"])
        self.assertEqual([row["id"] for row in store.search(location="San Francisco")[0]],
                         ["sf"])
        self.assertEqual([row["id"] for row in store.search(location="SF")[0]], ["sf"])

    def test_one_job_spelled_two_ways_by_two_sources_is_one_group(self):
        store.upsert_postings([_row("simplify", "NYC")], "simplify")
        store.upsert_postings([_row("board", "New York, NY")], "greenhouse")
        groups = dict(store.connect().execute("SELECT id, group_key FROM postings"))
        self.assertEqual(groups["simplify"], groups["board"])


if __name__ == "__main__":
    unittest.main()
