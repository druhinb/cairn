"""Watchlist files: what export writes, what import accepts, and how it merges."""
import json
import unittest

from helpers import app_client, temp_home
from test_cli import run_main

from cairn.core import paths, settings
from cairn.sources import watchlists

STRIPE = {"kind": "greenhouse", "location": "stripe", "company": "Stripe", "enabled": True}
RAMP = {"kind": "ashby", "location": "ramp", "company": "Ramp", "enabled": True}


def _file(*companies, **fields):
    return json.dumps({"format": "cairn-watchlist", "version": 1, "name": "Fintech",
                       "companies": list(companies), **fields})


class FileTest(unittest.TestCase):
    def test_export_leaves_out_what_is_turned_off_and_reads_back(self):
        data = watchlists.export([STRIPE, {**RAMP, "enabled": False}], name="Mine")
        self.assertEqual(data["companies"],
                         [{"company": "Stripe", "kind": "greenhouse", "location": "stripe"}])
        self.assertEqual(watchlists.parse(json.dumps(data)), ("Mine", [STRIPE]))

    def test_refuses_what_is_not_a_watchlist(self):
        for text, message in [
                ("not json", "not a Cairn watchlist"),
                (json.dumps({"companies": []}), "not a Cairn watchlist"),
                (_file(version=2), "version 2"),
                (_file({"kind": "rss", "location": "x"}), "company 1: kind should be"),
                (_file({"kind": "lever", "location": " "}), "company 1: missing location"),
                (_file(*[{"kind": "lever", "location": f"c{n}"} for n in range(501)]),
                 "more than 500")]:
            with self.subTest(message), self.assertRaisesRegex(
                    watchlists.WatchlistFileError, message):
                watchlists.parse(text)

    def test_merge_adds_new_boards_and_turns_on_followed_ones(self):
        current = [{**STRIPE, "location": "Stripe", "enabled": False}]
        merged, added, turned_on = watchlists.merge(current, [STRIPE, RAMP, RAMP])
        self.assertEqual(merged, [{**STRIPE, "location": "Stripe"}, RAMP])
        self.assertEqual((added, turned_on), (1, 1))
        self.assertFalse(current[0]["enabled"])


class ApiTest(unittest.TestCase):
    def setUp(self):
        self.enterContext(temp_home(watchlist=[STRIPE]))
        self.client = app_client(self)

    def test_export_then_import_into_another_watchlist(self):
        text = json.dumps(self.client.get("/api/watchlist/export").json())
        body = self.client.post("/api/watchlist/import",
                                json={"text": text, "watchlist": [RAMP]}).json()
        self.assertEqual((body["watchlist"], body["added"], body["already"]),
                         ([RAMP, STRIPE], 1, 0))
        self.assertEqual(settings.base().watchlist, [STRIPE])

    def test_a_bad_file_is_400_with_the_reason(self):
        response = self.client.post("/api/watchlist/import",
                                    json={"text": "{}", "watchlist": []})
        self.assertEqual((response.status_code, response.json()["error"]),
                         (400, "the file is not a Cairn watchlist"))


class CommandTest(unittest.TestCase):
    def test_import_saves_the_merged_watchlist(self):
        home = self.enterContext(temp_home(watchlist=[STRIPE]))
        path = home / "fintech.json"
        path.write_text(_file({"company": "Ramp", "kind": "ashby", "location": "ramp"},
                              {"company": "Stripe", "kind": "greenhouse",
                               "location": "stripe"}), encoding="utf-8")
        self.assertEqual(run_main("watchlist", "import", str(path)),
                         (0, "imported Fintech: 1 added, 0 turned back on, "
                             "1 already followed.\n"))
        self.assertIn('location = "ramp"', paths.config_file().read_text(encoding="utf-8"))
        code, out = run_main("watchlist", "export")
        self.assertEqual(len(json.loads(out)["companies"]), 2)

    def test_an_unreadable_file_exits_1(self):
        self.enterContext(temp_home())
        code, out = run_main("watchlist", "import", "/nonexistent/list.json")
        self.assertEqual(code, 1)
        self.assertIn("could not import /nonexistent/list.json", out)


if __name__ == "__main__":
    unittest.main()
