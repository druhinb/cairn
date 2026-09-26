"""`cairn ui`: a real server on a thread, with the browser and the window faked."""
import contextlib
import io
import unittest

import httpx
from helpers import temp_home

from cairn import events, server, window


class OpenUiTest(unittest.TestCase):
    def setUp(self):
        self.enterContext(temp_home())
        self.started = []
        self.seen = []
        real_start = server.start_in_thread

        def start(host, port):
            self.started.append(real_start(host, port))
            return self.started[-1]

        self.patch(server, "start_in_thread", start)
        self.baseline = len(events._subscribers)

    def patch(self, module, name, value):
        self.addCleanup(setattr, module, name, getattr(module, name))
        setattr(module, name, value)

    def fetch_status(self, page):
        """Open page as the window or browser would, then call the API with and
        without the cookie it set."""
        url = page.partition("/?")[0]
        with httpx.Client(timeout=5) as browser:
            opened = browser.get(page, follow_redirects=True)
            self.seen.append((url, opened.status_code,
                              browser.get(f"{url}/api/status").status_code,
                              httpx.get(f"{url}/api/status", timeout=5).status_code))
        self.assertEqual(len(events._subscribers), self.baseline + 1)

    def assert_served_and_stopped(self, port):
        self.assertEqual(self.seen, [(f"http://127.0.0.1:{port}", 200, 200, 403)])
        (srv, thread), = self.started
        self.assertTrue(srv.should_exit)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(events._subscribers), self.baseline)

    def test_no_window_opens_the_browser_and_stops_after_ctrl_c(self):
        opened = []
        self.patch(window.webbrowser, "open", opened.append)
        self.patch(window, "_wait_for_interrupt", lambda: self.fetch_status(opened[-1]))
        port = server.pick_port()
        with contextlib.redirect_stdout(io.StringIO()) as out:
            window.open_ui(no_window=True, port=port)
        self.assertIn(f"http://127.0.0.1:{port}/?t=", out.getvalue())
        self.assert_served_and_stopped(port)

    def test_window_shows_the_app_and_stops_when_closed(self):
        self.patch(window, "_show_window", lambda url, page: self.fetch_status(page))
        port = server.pick_port()
        window.open_ui(port=port)
        self.assert_served_and_stopped(port)

    def test_stops_the_server_when_the_window_fails(self):
        def broken(url, page):
            raise RuntimeError("no display")

        self.patch(window, "_show_window", broken)
        with self.assertRaisesRegex(RuntimeError, "no display"):
            window.open_ui()
        (srv, thread), = self.started
        self.assertFalse(thread.is_alive())

    def test_stops_the_server_when_the_log_cannot_be_opened(self):
        def unwritable(path):
            raise PermissionError(f"cannot write {path}")

        self.patch(window.logfile, "attach", unwritable)
        with self.assertRaises(PermissionError):
            window.open_ui(port=server.pick_port())
        (srv, thread), = self.started
        self.assertTrue(srv.should_exit)
        self.assertFalse(thread.is_alive())


if __name__ == "__main__":
    unittest.main()
