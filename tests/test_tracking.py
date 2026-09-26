"""Application tracking: follow-ups, the Today summary, the calendar feed, and the
routes behind them."""
import dataclasses
import datetime
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

from helpers import app_client, temp_home, user_home

from cairn import events, settings, store, tracking

NOW = time.time()


def _job(posting_id, company="Acme", title="Software Engineer", **fields):
    return {"id": posting_id, "company_name": company, "title": title,
            "url": f"https://jobs.test/{posting_id}", "locations": ["Seattle, WA"],
            "category": "Software", "active": True, "is_visible": True,
            "date_posted": NOW, "date_updated": NOW, **fields}


def _in(days=0, hours=0):
    return (datetime.datetime.now() + datetime.timedelta(days=days, hours=hours)).isoformat(
        timespec="seconds")


def _at(when, call, *args):
    """call(*args) with store._now() reading when."""
    with mock.patch.object(store, "_now", return_value=when):
        return call(*args)


def _settings_with(**changes):
    """The current settings with changes applied."""
    return dataclasses.replace(settings.get(), **changes)


def _epoch(text):
    return int(datetime.datetime.fromisoformat(text).timestamp())


def _store_application(posting_id, applied_at):
    """An applied row and its event written as a legacy import writes them, with
    applied_at kept as the text it was given."""
    with store.connect() as conn:
        conn.execute("INSERT INTO applications (posting_id, status, applied_at, note, "
                     "created_at, updated_at) VALUES (?, 'applied', ?, '', ?, ?)",
                     (posting_id, applied_at, applied_at, applied_at))
        conn.execute("INSERT INTO application_events (posting_id, status, at) "
                     "VALUES (?, 'applied', ?)", (posting_id, applied_at))


class FollowUpTest(unittest.TestCase):
    def setUp(self):
        self.enterContext(temp_home())
        store.upsert_postings([_job("old", "Acme"), _job("fresh", "Globex"),
                               _job("met", "Initech"), _job("next", "Hooli"),
                               _job("saved", "Umbrella")], "feed")

    def test_an_application_left_alone_for_the_window_needs_one(self):
        _at(_in(days=-20), store.set_status, "old", "applied")
        _at(_in(days=-3), store.set_status, "fresh", "applied")
        _at(_in(days=-30), store.set_status, "saved", "saved")
        stale = tracking.stale_applications()
        self.assertEqual([row["id"] for row in stale], ["old"])
        self.assertEqual(set(stale[0]), {"id", "company", "title", "status", "applied_at",
                                         "days_since", "last_event_at"})
        self.assertEqual((stale[0]["company"], stale[0]["status"], stale[0]["days_since"]),
                         ("Acme", "applied", 20))
        self.assertEqual([row["id"] for row in tracking.stale_applications(days=2)],
                         ["old", "fresh"])

    def test_the_window_comes_from_follow_up_days(self):
        _at(_in(days=-5), store.set_status, "old", "applied")
        self.assertEqual(tracking.stale_applications(), [])
        with mock.patch.object(settings, "get", return_value=_settings_with(follow_up_days=4)):
            self.assertEqual([row["id"] for row in tracking.stale_applications()], ["old"])

    def test_a_later_event_restarts_the_wait(self):
        _at(_in(days=-20), store.set_status, "old", "applied")
        store.add_stage("old", "screen", _in(days=-2))
        self.assertEqual(tracking.stale_applications(), [])

    def test_an_interview_that_passed_with_no_word_since_needs_one(self):
        for posting_id in ("met", "next", "old"):
            _at(_in(days=-10), store.set_status, posting_id, "interviewing")
        store.add_stage("met", "onsite", _in(days=-4))
        store.add_stage("next", "screen", _in(days=-5))
        store.add_stage("next", "final", _in(days=2))
        store.add_stage("old", "screen", _in(days=-(tracking.STAGE_GRACE_DAYS - 1)))
        stale = tracking.stale_applications()
        self.assertEqual([(row["id"], row["days_since"]) for row in stale], [("met", 4)])
        _at(_in(), store.set_status, "met", "offer")
        self.assertEqual(tracking.stale_applications(), [])

    def test_a_stored_time_that_is_not_iso_is_passed_over(self):
        _store_application("old", "07/27/2026")
        _store_application("fresh", "2026-07-27T09:00:00Z")
        _at(_in(days=-20), store.set_status, "met", "applied")
        stale = tracking.stale_applications()
        self.assertEqual([row["id"] for row in stale], ["fresh", "met"])
        utc = datetime.datetime(2026, 7, 27, 9, tzinfo=datetime.timezone.utc)
        self.assertEqual(stale[0]["days_since"],
                         (datetime.datetime.now() - utc.astimezone().replace(tzinfo=None)).days)
        self.assertEqual(tracking.follow_up_note(stale),
                         "2 applications need a follow-up: Globex, Initech")

    def test_the_note_names_the_count_and_two_companies(self):
        self.assertEqual(tracking.follow_up_note([]), "")
        rows = [{"company": name} for name in ("Acme", "Globex", "Initech")]
        self.assertEqual(tracking.follow_up_note(rows),
                         "3 applications need a follow-up: Acme, Globex")
        self.assertEqual(tracking.follow_up_note(rows[:1]),
                         "1 application needs a follow-up: Acme")
        self.assertEqual(tracking.follow_up_note([{"company": None}]),
                         "1 application needs a follow-up")


class TodayTest(unittest.TestCase):
    def setUp(self):
        self.enterContext(temp_home())

    def test_an_empty_home(self):
        self.assertEqual(tracking.today(), {
            "new_since_last_visit": {"count": 0, "run_id": None}, "picks": [],
            "follow_ups": [], "interviews": [], "applied_this_week": 0,
            "closed_recently": 0, "skills": [],
            "cost": {"rank": 0, "summary": 0, "onboard": 0, "total": 0, "prompt_chars": 0}})

    def _seed(self):
        store.upsert_postings([_job(f"p{n}", f"Co{n}") for n in range(8)], "feed")
        earlier = store.start_run()
        store.save_scores([{"id": "p7", "fit": 99, "tier": 99, "below_floor": False}], earlier)
        store.finish_run(earlier, "ok", {})
        latest = store.start_run()
        store.save_scores([{"id": f"p{n}", "fit": 50 + n, "tier": 50 + n, "below_floor": False}
                           for n in range(7)], latest)
        store.finish_run(latest, "ok", {})
        store.set_status("p6", "saved")
        store.set_status("p5", "applied")
        store.save_link_checks([("p0", "closed")])
        store.add_stage("p5", "screen", _in(days=2), "bring questions")
        store.add_stage("p5", "onsite", _in(days=10))
        store.set_keywords("p1", "MET: Python\nMISSING: Rust, Go")
        store.record_call("rank", "model", 100, 10)
        return earlier, latest

    def test_scores_from_before_run_ids_still_give_picks(self):
        store.upsert_postings([_job(f"p{n}", f"Co{n}") for n in range(3)], "feed")
        store.save_scores([{"id": f"p{n}", "fit": 60 + n, "tier": 60 + n, "below_floor": False}
                           for n in range(3)], None)
        store.set_status("p2", "applied")
        self.assertEqual([job["id"] for job in tracking.today()["picks"]], ["p1", "p0"])

    def test_a_home_with_runs_applications_and_stages(self):
        _, latest = self._seed()
        summary = tracking.today()
        # p0's closed link keeps it out of the count
        self.assertEqual(summary["new_since_last_visit"], {"count": 6, "run_id": latest})
        # and out of the picks
        self.assertEqual([job["id"] for job in summary["picks"]], ["p4", "p3", "p2", "p1"])
        self.assertEqual([(i["id"], i["stage"], i["note"]) for i in summary["interviews"]],
                         [("p5", "screen", "bring questions")])
        self.assertEqual(set(summary["interviews"][0]),
                         {"event_id", "id", "company", "title", "stage", "at", "note"})
        self.assertEqual((summary["applied_this_week"], summary["closed_recently"]), (1, 1))
        self.assertEqual([s["skill"] for s in summary["skills"]], ["go", "rust"])
        self.assertEqual(summary["cost"]["rank"], 1)
        self.assertEqual(summary["follow_ups"], [])

    def test_new_since_counts_the_runs_finished_after_a_visit(self):
        _, latest = self._seed()
        long_ago = datetime.datetime.now() - datetime.timedelta(days=1)
        self.assertEqual(tracking.today(long_ago)["new_since_last_visit"],
                         {"count": 7, "run_id": latest})
        later = datetime.datetime.now() + datetime.timedelta(minutes=1)
        self.assertEqual(tracking.today(later)["new_since_last_visit"],
                         {"count": 0, "run_id": None})


def _unfold(text):
    """RFC 5545 content lines, each CRLF-ended and unfolded, checking every physical
    line on the way."""
    assert text.endswith("\r\n"), "the calendar must end with CRLF"
    physical = text[:-2].split("\r\n")
    for line in physical:
        assert "\n" not in line and "\r" not in line, f"bare line break in {line!r}"
        assert len(line.encode()) <= 75, f"{len(line.encode())} octets: {line!r}"
    lines = []
    for line in physical:
        if line.startswith(" "):
            lines[-1] += line[1:]
        else:
            lines.append(line)
    return lines


def _unescape(value):
    out, chars = [], iter(value)
    for char in chars:
        if char == "\\":
            following = next(chars)
            out.append("\n" if following in "nN" else following)
        else:
            out.append(char)
    return "".join(out)


def _events(text):
    """[{property: value}] for each VEVENT, checking BEGIN/END balance."""
    lines, stack, found = _unfold(text), [], []
    for line in lines:
        name, _, value = line.partition(":")
        if name == "BEGIN":
            stack.append(value)
            if value == "VEVENT":
                found.append({})
        elif name == "END":
            assert stack and stack.pop() == value, f"unbalanced END:{value}"
        elif stack and stack[-1] == "VEVENT":
            found[-1][name] = value
    assert not stack, f"never ended: {stack}"
    assert lines[0] == "BEGIN:VCALENDAR" and lines[-1] == "END:VCALENDAR"
    return found


class CalendarTest(unittest.TestCase):
    def setUp(self):
        self.enterContext(temp_home())
        store.upsert_postings([_job("a", "Ångström, Inc; Labs"), _job("b", "Globex")], "feed")

    def test_an_empty_calendar_is_valid(self):
        self.assertEqual(_events(tracking.ics()), [])
        self.assertIn("VERSION:2.0", _unfold(tracking.ics()))

    def test_each_stage_is_an_hour_long_event(self):
        note = "Prep: " + ", ".join(f"topic {n}; ünïcode" for n in range(20))
        self.assertGreater(len(note), 200)
        created = "2026-09-01T09:00:00"
        first = _at(created, store.add_stage, "a", "onsite", _in(days=10), note)
        store.add_stage("b", "screen", _in(days=1))
        store.add_stage("b", "final", _in(days=200))
        found = _events(tracking.ics())
        self.assertEqual(len(found), 2)
        self.assertEqual(len({event["UID"] for event in found}), 2)
        uid = f"stage-{first['id']}-{_epoch(created)}@cairn"
        onsite = next(e for e in found if e["UID"] == uid)
        self.assertEqual(onsite["SEQUENCE"], "0")
        self.assertEqual(_unescape(onsite["SUMMARY"]), "Ångström, Inc; Labs · Onsite")
        self.assertEqual(_unescape(onsite["DESCRIPTION"]), f"{note}\n\nhttps://jobs.test/a")
        begins = datetime.datetime.fromisoformat(first["at"]).astimezone(datetime.timezone.utc)
        self.assertEqual(onsite["DTSTART"], begins.strftime("%Y%m%dT%H%M%SZ"))
        self.assertEqual(onsite["DTEND"], (begins + datetime.timedelta(hours=1))
                         .strftime("%Y%m%dT%H%M%SZ"))
        self.assertRegex(onsite["DTSTAMP"], r"^\d{8}T\d{6}Z$")

    def test_the_window_reaches_back_a_month(self):
        recent = store.add_stage("a", "screen", _in(days=-(tracking.CALENDAR_DAYS_BACK - 1)))
        store.add_stage("a", "screen", _in(days=-(tracking.CALENDAR_DAYS_BACK + 1)))
        store.add_stage("b", "onsite", _in(days=20))
        self.assertEqual(len(_events(tracking.ics())), 2)
        found = _events(tracking.ics(days_ahead=10))
        self.assertEqual([e["UID"].split("-")[1] for e in found], [str(recent["id"])])

    def test_an_edit_raises_the_sequence_and_a_reused_id_gets_a_new_uid(self):
        first = _at("2026-09-01T09:00:00", store.add_stage, "a", "screen", _in(days=1))
        _at("2026-09-01T09:01:30", store.update_stage, first["id"], None, None, "moved")
        [event] = _events(tracking.ics())
        self.assertEqual(event["SEQUENCE"], "90")
        store.delete_stage(first["id"])
        second = _at("2026-09-02T09:00:00", store.add_stage, "a", "screen", _in(days=1))
        self.assertEqual(second["id"], first["id"])
        [again] = _events(tracking.ics())
        self.assertNotEqual(again["UID"], event["UID"])

    def test_a_closed_application_leaves_the_calendar(self):
        store.add_stage("a", "screen", _in(days=1))
        store.set_status("a", "rejected")
        self.assertEqual(_events(tracking.ics()), [])

    def test_control_characters_become_spaces(self):
        store.add_stage("a", "screen", _in(days=1), "line one\x00\x07\ttab\x1b\x7f\r\nline two")
        [event] = _events(tracking.ics())
        self.assertEqual(_unescape(event["DESCRIPTION"]),
                         "line one   tab  \nline two\n\nhttps://jobs.test/a")

    @unittest.skipIf(sys.platform == "win32", "Windows has no time.tzset to switch zones")
    def test_local_times_across_dst_changes_convert_by_the_zone_rules(self):
        # tzset runs again once TZ is restored; cleanups run last in, first out
        self.addCleanup(time.tzset)
        self.enterContext(mock.patch.dict(os.environ, {"TZ": "America/Los_Angeles"}))
        time.tzset()
        zone = ZoneInfo("America/Los_Angeles")
        # 01:30 on the fall-back day happens twice; 02:30 on the spring-forward day never
        for local, utc in ((datetime.datetime(2026, 11, 1, 1, 30), "20261101T083000Z"),
                           (datetime.datetime(2026, 11, 1, 3, 0), "20261101T110000Z"),
                           (datetime.datetime(2026, 3, 8, 2, 30), "20260308T103000Z"),
                           (datetime.datetime(2026, 3, 8, 5, 0), "20260308T120000Z")):
            self.assertEqual(tracking._utc(local), utc)
            self.assertEqual(tracking._utc(local.replace(tzinfo=zone)), utc)

    def test_folding_never_splits_a_character(self):
        line = "DESCRIPTION:" + "é" * 100
        folded = tracking._fold(line)
        self.assertEqual(folded.replace("\r\n ", ""), line)
        for piece in folded.split("\r\n"):
            self.assertLessEqual(len(piece.encode()), 75)
            piece.encode().decode()


class RouteTest(unittest.TestCase):
    def setUp(self):
        self.enterContext(temp_home())
        store.upsert_postings([_job("a"), _job("b", "Globex")], "feed")
        self.client = app_client(self)
        self.heard = []
        self.addCleanup(events.subscribe(self.heard.append))

    def kinds(self):
        return [(event.kind, event.data) for event in self.heard
                if event.kind in ("application_changed", "tracking_changed")]

    def call(self, method, path, status, **kwargs):
        response = self.client.request(method, path, **kwargs)
        self.assertEqual(response.status_code, status, response.text)
        return response.json()

    def test_stages(self):
        event = self.call("POST", "/api/jobs/a/stages", 200,
                          json={"stage": "screen", "at": "2026-10-01T14:00", "note": "Kim"})
        self.assertEqual((event["stage"], event["at"], event["note"]),
                         ("screen", "2026-10-01T14:00:00", "Kim"))
        self.assertEqual(self.kinds(), [
            ("application_changed", {"id": "a", "status": "interviewing"}),
            ("tracking_changed", {"id": "a"})])
        self.heard.clear()
        self.call("POST", "/api/jobs/a/stages", 200, json={"stage": "oa", "at": "2026-10-02"})
        self.assertEqual(self.kinds(), [("tracking_changed", {"id": "a"})])
        self.call("POST", "/api/jobs/nope/stages", 404, json={"stage": "oa", "at": "2026-10-02"})
        self.call("POST", "/api/jobs/a/stages", 400, json={"stage": "lunch", "at": "2026-10-02"})
        self.call("POST", "/api/jobs/a/stages", 400, json={"stage": "oa", "at": "soon"})
        self.call("POST", "/api/jobs/a/stages", 400,
                  json={"stage": "oa", "at": "0001-01-01T00:00:00+05:00"})
        self.call("POST", "/api/jobs/a/stages", 400, json={"stage": "oa"})
        changed = self.call("PATCH", f"/api/stages/{event['id']}", 200,
                            json={"stage": "onsite", "note": "moved"})
        self.assertEqual((changed["stage"], changed["note"]), ("onsite", "moved"))
        self.call("PATCH", f"/api/stages/{event['id']}", 400, json={"at": "later"})
        self.heard.clear()
        refused = self.call("PATCH", f"/api/stages/{event['id']}", 400, json={})
        self.assertEqual(refused["error"], "nothing to change")
        self.assertEqual(self.kinds(), [])
        self.call("PATCH", "/api/stages/9999", 404, json={"note": "x"})
        self.call("DELETE", f"/api/stages/{event['id']}", 200)
        self.call("DELETE", f"/api/stages/{event['id']}", 404)

    def test_checklist(self):
        self.assertEqual(self.call("GET", "/api/jobs/a/checklist", 200), [])
        self.call("GET", "/api/jobs/nope/checklist", 404)
        first = self.call("POST", "/api/jobs/a/checklist", 200, json={"label": "Portfolio"})
        second = self.call("POST", "/api/jobs/a/checklist", 200, json={"label": "Transcript"})
        self.assertEqual({k: first[k] for k in ("label", "done", "position")},
                         {"label": "Portfolio", "done": False, "position": 0})
        self.call("POST", "/api/jobs/a/checklist", 422, json={"label": " "})
        self.call("POST", "/api/jobs/nope/checklist", 404, json={"label": "x"})
        self.assertTrue(self.call("PATCH", f"/api/checklist/{first['id']}", 200,
                                  json={"done": True})["done"])
        self.call("PATCH", f"/api/checklist/{first['id']}", 422, json={"label": ""})
        self.call("PATCH", "/api/checklist/9999", 404, json={"done": True})
        order = self.call("PUT", "/api/jobs/a/checklist/order", 200,
                          json={"ids": [second["id"], first["id"]]})
        self.assertEqual([item["label"] for item in order], ["Transcript", "Portfolio"])
        self.call("PUT", "/api/jobs/a/checklist/order", 422, json={"ids": [first["id"]]})
        self.call("PUT", "/api/jobs/nope/checklist/order", 404, json={"ids": []})
        self.call("DELETE", f"/api/checklist/{first['id']}", 200)
        self.call("DELETE", f"/api/checklist/{first['id']}", 404)
        self.assertTrue(all(kind == "tracking_changed" and data == {"id": "a"}
                            for kind, data in self.kinds()))
        self.assertEqual(len(self.kinds()), 5)

    def test_attachments(self):
        home = self.enterContext(tempfile.TemporaryDirectory())
        self.enterContext(user_home(home))
        (Path(home) / "cv.pdf").write_text("cv", encoding="utf-8")
        added = self.call("POST", "/api/jobs/a/attachments", 200,
                          json={"label": "CV", "path": str(Path(home) / "cv.pdf")})
        self.assertEqual(added["path"], os.path.realpath(Path(home) / "cv.pdf"))
        self.assertEqual(self.call("GET", "/api/jobs/a/attachments", 200), [added])
        self.call("GET", "/api/jobs/nope/attachments", 404)
        self.call("POST", "/api/jobs/a/attachments", 400, json={"path": home})
        self.call("POST", "/api/jobs/a/attachments", 400, json={"path": "/etc/hosts"})
        self.call("POST", "/api/jobs/nope/attachments", 404, json={"path": "/etc/hosts"})
        self.call("DELETE", f"/api/attachments/{added['id']}", 200)
        self.call("DELETE", f"/api/attachments/{added['id']}", 404)
        self.assertEqual(self.kinds(), [("tracking_changed", {"id": "a"})] * 2)

    def test_follow_ups_today_and_the_calendar(self):
        _at(_in(days=-30), store.set_status, "b", "applied")
        self.assertEqual([row["id"] for row in self.call("GET", "/api/tracking/follow-ups", 200)],
                         ["b"])
        run_id = store.start_run()
        store.save_scores([{"id": "a", "fit": 80, "tier": 70, "below_floor": False}], run_id)
        store.finish_run(run_id, "ok", {})
        today = self.call("GET", "/api/today", 200)
        jobs_row = self.call("GET", "/api/jobs", 200)["rows"][0]
        self.assertEqual(today["picks"], [jobs_row])
        self.assertEqual(today["new_since_last_visit"], {"count": 1, "run_id": run_id})
        since = int((time.time() + 60) * 1000)
        self.assertEqual(self.call("GET", f"/api/today?since={since}", 200)
                         ["new_since_last_visit"], {"count": 0, "run_id": None})
        self.call("GET", "/api/today?since=yesterday", 400)
        self.call("GET", "/api/today?since=0001-01-01T00:00:00%2B05:00", 400)
        store.add_stage("a", "screen", _in(days=1))
        response = self.client.get("/api/calendar.ics")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["content-type"], "text/calendar; charset=utf-8")
        self.assertEqual(response.headers["content-disposition"],
                         'attachment; filename="cairn.ics"')
        self.assertEqual(len(_events(response.text)), 1)
        store.add_stage("a", "onsite", _in(days=5))
        self.assertEqual(len(_events(self.client.get("/api/calendar.ics?days=2").text)), 1)
        for days in (0, 366, "many"):
            self.call("GET", f"/api/calendar.ics?days={days}", 400)

    def test_since_takes_an_offset_or_a_date(self):
        run_id = store.start_run()
        store.finish_run(run_id, "ok", {})
        utc = datetime.datetime.now(datetime.timezone.utc)
        for since, found in ((utc - datetime.timedelta(minutes=5), run_id),
                             (utc + datetime.timedelta(minutes=5), None),
                             (utc.astimezone(datetime.timezone(datetime.timedelta(hours=5)))
                              - datetime.timedelta(minutes=5), run_id)):
            summary = self.call("GET", "/api/today", 200, params={"since": since.isoformat()})
            self.assertEqual(summary["new_since_last_visit"]["run_id"], found, since)
        today = datetime.date.today()
        for day, found in ((today, run_id), (today + datetime.timedelta(days=1), None)):
            summary = self.call("GET", "/api/today", 200, params={"since": day.isoformat()})
            self.assertEqual(summary["new_since_last_visit"]["run_id"], found, day)

    def test_a_change_needs_the_session(self):
        self.client.cookies.clear()
        self.call("POST", "/api/jobs/a/checklist", 403, json={"label": "x"})


if __name__ == "__main__":
    unittest.main()
