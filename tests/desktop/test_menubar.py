"""The menu bar's count of postings ranked since the window was last in front, its
labels, when it attaches, and its threads. The AppKit side needs a GUI session and is
checked by hand."""
import io
import threading
import unittest
import urllib.error
from unittest import mock

from cairn.core import events
from cairn.desktop import menubar
from cairn.server.app import SESSION


def _run(run_id, finished_at, ranked):
    return {"id": run_id, "finished_at": finished_at, "ranked": ranked}


class UnseenTest(unittest.TestCase):
    def test_counts_runs_that_finished_after_the_last_look(self):
        unseen = menubar.Unseen("2026-09-25T08:00:00")
        unseen.add(_run(1, "2026-09-25T07:00:00", 5))
        unseen.add(_run(2, "2026-09-25T09:00:00", 4))
        unseen.add(_run(2, "2026-09-25T09:00:00", 4))
        unseen.add(_run(3, None, 9))
        unseen.add(_run(4, "2026-09-25T10:00:00", 0))
        unseen.add(None)
        self.assertEqual(unseen.count(), 4)
        unseen.add(_run(5, "2026-09-25T11:00:00", 3))
        self.assertEqual(unseen.count(), 7)

    def test_looking_clears_the_count(self):
        unseen = menubar.Unseen("2026-09-25T08:00:00")
        unseen.add(_run(2, "2026-09-25T09:00:00", 4))
        unseen.looked("2026-09-25T09:30:00")
        self.assertEqual(unseen.count(), 0)
        unseen.add(_run(2, "2026-09-25T09:00:00", 4))
        self.assertEqual(unseen.count(), 0)


class LabelTest(unittest.TestCase):
    def test_unseen_and_badge(self):
        self.assertEqual(menubar.unseen_label(0), "Nothing new since you looked")
        self.assertEqual(menubar.unseen_label(3), "3 new since you looked")
        self.assertIsNone(menubar.badge(0))
        self.assertEqual(menubar.badge(12), "12")

    def test_update(self):
        found = {"current": "0.2.0", "latest": "0.3.0", "newer": True}
        self.assertEqual(menubar.update_label(found), "Update to 0.3.0 available")
        self.assertEqual(menubar.update_label({**found, "newer": False}),
                         "Cairn 0.2.0 is up to date")
        self.assertEqual(menubar.update_label(None), "Couldn't check for updates")


class BrowserView:
    AppDelegate = object
    _shared_app_delegate = None


class AttachTest(unittest.TestCase):
    def setUp(self):
        self.warnings = []
        self.addCleanup(events.subscribe(
            lambda e: e.kind == "warn" and self.warnings.append(e.data["text"])))
        self.enterContext(mock.patch.object(menubar, "_browser_view", lambda: BrowserView))

    def test_the_window_opens_without_its_menu_bar_item(self):
        with mock.patch.object(menubar, "MenuBar", side_effect=ImportError("no AppKit")):
            self.assertIsNone(menubar.attach("http://127.0.0.1:1", object()))
        self.assertEqual(self.warnings, ["no menu-bar item: ImportError: no AppKit"])

    def test_a_pywebview_without_the_hooks_gets_the_window_alone(self):
        for missing in menubar.PYWEBVIEW_HOOKS:
            changed = type("BrowserView", (), {h: None for h in menubar.PYWEBVIEW_HOOKS
                                               if h != missing})
            with self.subTest(missing), \
                    mock.patch.object(menubar, "_browser_view", lambda: changed), \
                    mock.patch.object(menubar, "MenuBar") as built:
                self.assertIsNone(menubar.attach("http://127.0.0.1:1", object()))
                built.assert_not_called()
        self.assertEqual(self.warnings,
                         ["menu bar unavailable with this pywebview version"] * 2)

    def test_a_pywebview_with_the_hooks_gets_the_item(self):
        with mock.patch.object(menubar, "MenuBar") as built:
            self.assertIs(menubar.attach("http://127.0.0.1:1", "window"), built.return_value)
        built.assert_called_once_with("http://127.0.0.1:1", "window")


def _bare_menubar():
    """A MenuBar without AppKit: only the state the threads touch."""
    bar = menubar.MenuBar.__new__(menubar.MenuBar)
    bar._stop, bar._refresh = threading.Event(), threading.Event()
    bar._unsubscribe = None
    bar.call_after = lambda fn, *args: fn(*args)
    return bar


class ThreadsTest(unittest.TestCase):
    def test_a_finished_run_refreshes_the_badge_at_once(self):
        bar = _bare_menubar()
        polled = []
        bar.local = mock.Mock(get=lambda path: polled.append(path) or {"latest_run": None})
        thread = threading.Thread(target=bar._poll)
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(bar.stop)
        bar._on_event(events.Event("info", 0, {}))
        bar._on_event(events.Event("run_done", 0, {"run_id": 1}))
        for _ in range(100):
            if len(polled) >= 2:
                break
            threading.Event().wait(0.01)
        self.assertEqual(polled, ["/api/status", "/api/status"])

    def test_a_run_that_cannot_start_brings_the_window_forward(self):
        bar = _bare_menubar()
        warnings = []
        self.addCleanup(events.subscribe(
            lambda e: e.kind == "warn" and warnings.append(e.data["text"])))
        bar.local = mock.Mock(post=mock.Mock(side_effect=urllib.error.URLError("refused")))
        bar.open = mock.Mock()
        bar._start_run()
        bar.open.assert_called_once_with()
        self.assertEqual(warnings, ["could not start a run: URLError: <urlopen error refused>"])

    def test_requests_to_the_app_carry_its_session_and_header(self):
        local = menubar._Local("http://127.0.0.1:1")
        sent = []
        local._opener = mock.Mock(
            open=lambda request, timeout: sent.append(request) or io.BytesIO(b"{}"))
        self.assertEqual(local.post("/api/run"), {})
        self.assertEqual(local.get("/api/status"), {})
        for request in sent:
            with self.subTest(request.get_method()):
                self.assertEqual(request.get_header("Cookie"), f"cairn_session_1={SESSION}")
                self.assertEqual(request.get_header("X-cairn"), "1")


if __name__ == "__main__":
    unittest.main()
