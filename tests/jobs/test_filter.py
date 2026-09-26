"""Relevance filtering: a posting needs both its category and its title to look right.

On the live feed, category alone admitted "Meteorologist" (tagged AI/ML/Data
upstream) and title alone admitted "Photonics Engineer".
"""
import dataclasses
import unittest
from unittest import mock

from helpers import temp_home

from cairn import sources
from cairn.core import settings
from cairn.jobs import fetch


def _job(title, category="Software", degrees=None):
    return {"active": True, "is_visible": True, "title": title,
            "category": category, "locations": ["Seattle, WA"],
            "degrees": [] if degrees is None else degrees}


class DegreeTest(unittest.TestCase):
    """`degrees` lists the degrees a posting accepts, so a list without yours excludes you."""

    def setUp(self):
        self.enterContext(temp_home(degrees_held=["Bachelor's", "Associate's"]))

    def test_graduate_only_roles_are_dropped(self):
        for degrees in ([" PhD".strip()], ["Master's"], ["Master's", "PhD"]):
            self.assertFalse(
                fetch._relevant(_job("Quantitative Researcher", "Quant", degrees)),
                degrees)

    def test_roles_open_to_bachelors_are_kept(self):
        for degrees in ([], ["Bachelor's"], ["Bachelor's", "Master's"],
                        ["Bachelor's", "Master's", "PhD"]):
            self.assertTrue(fetch._relevant(_job("Software Engineer", "Software", degrees)),
                            degrees)

    def test_unstated_degrees_are_kept(self):
        self.assertTrue(fetch._relevant(_job("Software Engineer", "Software", [])))


class RelevanceTest(unittest.TestCase):
    def test_keeps_a_plain_new_grad_swe_role(self):
        self.assertTrue(fetch._relevant(_job("Software Engineer")))
        self.assertTrue(fetch._relevant(_job("Graduate Software Engineer")))

    def test_keeps_quant_and_ml_roles(self):
        self.assertTrue(fetch._relevant(_job("Quantitative Researcher", "Quant")))
        self.assertTrue(fetch._relevant(_job("ML Engineer", "AI/ML/Data")))

    def test_right_category_but_non_technical_title_is_dropped(self):
        # upstream tags these AI/ML/Data
        self.assertFalse(fetch._relevant(_job("Meteorologist", "AI/ML/Data")))
        self.assertFalse(fetch._relevant(_job("Survey Mapping Drafter", "AI/ML/Data")))
        self.assertFalse(fetch._relevant(_job("Data Analyst 2", "AI/ML/Data")))

    def test_engineer_title_in_the_wrong_field_is_dropped(self):
        self.assertFalse(fetch._relevant(_job("Photonics Engineer 1")))
        self.assertFalse(fetch._relevant(_job("Field Service Engineer")))
        self.assertFalse(fetch._relevant(_job("Mechanical Engineer")))

    def test_hardware_is_dropped_per_profile_preference(self):
        self.assertFalse(fetch._relevant(_job("Graduate Firmware Engineer", "Hardware")))
        self.assertFalse(fetch._relevant(_job("Junior FPGA Developer", "Hardware")))

    def test_wrong_seniority_still_excluded(self):
        self.assertFalse(fetch._relevant(_job("Senior Software Engineer")))
        self.assertFalse(fetch._relevant(_job("Software Engineer Intern")))

    def test_inactive_or_hidden_is_dropped(self):
        job = _job("Software Engineer")
        self.assertFalse(fetch._relevant({**job, "active": False}))
        self.assertFalse(fetch._relevant({**job, "is_visible": False}))


class DroppedByTest(unittest.TestCase):
    """why_nothing reports the rule dropped_by names, which must agree with _relevant."""

    def setUp(self):
        self.enterContext(temp_home(degrees_held=["Bachelor's"], location_allow=["WA"]))

    def test_each_rule_is_named_in_the_order_it_applies(self):
        cases = {
            "Software Engineer": None,
            "Senior Mechanical Engineer": "exclude",
            "Mechanical Engineer": "field exclude",
            "Software Engineer Intern": "intern term",
            "Accountant": "title keyword",
        }
        for title, rule in cases.items():
            with self.subTest(title=title):
                job = _job(title)
                self.assertEqual(fetch.dropped_by(job), rule)
                self.assertEqual(fetch._relevant(job), rule is None)
        self.assertEqual(fetch.dropped_by(_job("Software Engineer", "Marketing")), "category")
        self.assertEqual(fetch.dropped_by(_job("Software Engineer", degrees=["PhD"])), "degree")
        self.assertEqual(fetch.dropped_by({**_job("Software Engineer"),
                                           "locations": ["Austin, TX"]}), "location")


class BoardRowTest(unittest.TestCase):
    """A board row has no category, so relevance rests on the title alone."""

    def test_a_workday_new_grad_role_is_kept_and_a_senior_one_dropped(self):
        titles = ["Software Engineer, New Grad", "Senior Software Engineer"]
        page = {"total": 2, "jobPostings": [
            {"title": title, "externalPath": f"/job/US-WA-Seattle/R{i}",
             "locationsText": "US, WA, Seattle", "postedOn": "Posted Today"}
            for i, title in enumerate(titles)]}
        with temp_home(), mock.patch.object(sources.web, "post_json",
                                            lambda url, body, timeout=None: page):
            new_grad, senior = sources.workday("acme.wd5/External", "Acme")
            self.assertTrue(fetch._relevant(new_grad))
            self.assertFalse(fetch._relevant(senior))


class InternshipTest(unittest.TestCase):
    """The season lives in the feed's `terms` field.

    Matching on the title found none and hid 446 Fall 2026 postings, since nearly
    every title is a bare "Software Engineer Intern".
    """

    def setUp(self):
        self.enterContext(temp_home(
            wanted_intern_terms=["fall 2026", "winter 2027", "spring 2027"]))

    def _intern(self, terms):
        job = _job("Software Engineer Intern")
        job["terms"] = terms
        return job

    def test_a_wanted_term_is_kept(self):
        self.assertTrue(fetch._relevant(self._intern(["Fall 2026"])))
        self.assertTrue(fetch._relevant(self._intern(["Winter 2027"])))

    def test_summer_is_dropped(self):
        self.assertFalse(fetch._relevant(self._intern(["Summer 2026"])))
        self.assertFalse(fetch._relevant(self._intern(["Summer 2027"])))

    def test_a_past_term_is_dropped(self):
        self.assertFalse(fetch._relevant(self._intern(["Spring 2026"])))

    def test_an_internship_with_no_terms_is_dropped(self):
        """No stated term means the summer cohort."""
        self.assertFalse(fetch._relevant(self._intern([])))

    def test_one_wanted_term_among_several_is_enough(self):
        self.assertTrue(fetch._relevant(self._intern(["Summer 2026", "Fall 2026"])))

    def test_term_matching_ignores_case_and_padding(self):
        self.assertTrue(fetch._relevant(self._intern(["  fall 2026  "])))

    def test_non_internships_are_unaffected_by_terms(self):
        job = _job("Software Engineer")
        self.assertTrue(fetch._relevant(job))

    def test_co_op_counts_as_an_internship(self):
        job = _job("Software Engineering Co-op")
        job["terms"] = ["Summer 2026"]
        self.assertFalse(fetch._relevant(job))
        job["terms"] = ["Fall 2026"]
        self.assertTrue(fetch._relevant(job))

    def test_no_wanted_terms_drops_every_internship(self):
        settings.use(dataclasses.replace(settings.get(), wanted_intern_terms=[]))
        self.assertFalse(fetch._relevant(self._intern(["Fall 2026"])))

    def test_disabling_the_toggle_drops_all_internships(self):
        settings.use(dataclasses.replace(settings.get(),
                                         include_off_season_internships=False))
        self.assertFalse(fetch._relevant(self._intern(["Fall 2026"])))


if __name__ == "__main__":
    unittest.main()


class MultiSourceTest(unittest.TestCase):
    """Ids are per-repo, so the same posting from two feeds must dedupe by URL."""

    def test_the_same_url_from_two_sources_is_kept_once(self):
        rows = {
            "a": [{"id": "1", "url": "https://x.test/job/5", "company_name": "Acme"}],
            "b": [{"id": "999", "url": "https://x.test/job/5/", "company_name": "Acme"}],
        }
        self.assertEqual(_load_with(rows), 1)

    def test_tracking_query_strings_do_not_defeat_the_match(self):
        rows = {
            "a": [{"id": "1", "url": "https://x.test/job/5?src=simplify"}],
            "b": [{"id": "2", "url": "https://x.test/job/5?utm=other"}],
        }
        self.assertEqual(_load_with(rows), 1)

    def test_genuinely_different_postings_both_survive(self):
        rows = {
            "a": [{"id": "1", "url": "https://x.test/job/5"}],
            "b": [{"id": "2", "url": "https://x.test/job/6"}],
        }
        self.assertEqual(_load_with(rows), 2)

    def test_the_richer_first_source_wins(self):
        rows = {
            "a": [{"id": "1", "url": "https://x.test/j", "category": "Software"}],
            "b": [{"id": "2", "url": "https://x.test/j"}],
        }
        items = _load_items(rows)
        self.assertEqual(items[0]["category"], "Software")

    def test_a_posting_with_no_url_falls_back_to_company_and_title(self):
        rows = {
            "a": [{"id": "1", "company_name": "Acme", "title": "SWE"}],
            "b": [{"id": "2", "company_name": "acme", "title": "swe"}],
        }
        self.assertEqual(_load_with(rows), 1)


def _feed(name):
    return f"https://raw.githubusercontent.com/test/{name}/dev/listings.json"


def _load_items(rows_by_source):
    specs = [{"kind": "github", "location": _feed(name)} for name in rows_by_source]
    by_url = {_feed(name): rows for name, rows in rows_by_source.items()}
    with temp_home(sources=specs), mock.patch.object(
            sources.web, "get_json", lambda url, timeout=None, limit=None: by_url[url]):
        return [job for _, rows in fetch._load_all() for job in rows]


def _load_with(rows_by_source):
    return len(_load_items(rows_by_source))
