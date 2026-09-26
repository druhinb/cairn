"""Requests reach only the public internet, within their size and time limits.

Requests go to a local server standing in for the web: socket.getaddrinfo answers
with a public address unless a test names another, and net._connect dials the local
server whatever address it is handed, so every check before the connection runs as
it would live. Other test modules use LocalWeb for the same stand-in.
"""
import http.server
import socket
import socketserver
import threading
import time
import unittest
from unittest import mock

from cairn import __version__
from cairn.core import net

PUBLIC_IP = "93.184.216.34"


class _Handler(http.server.BaseHTTPRequestHandler):
    def _answer(self):
        web = self.server.web
        if self.command == "POST":
            web.bodies.append(self.rfile.read(int(self.headers.get("Content-Length", 0))))
        url = f"https://{self.headers['Host']}{self.path}"
        web.requested.append(url)
        web.methods.append(self.command)
        web.hosts.append(self.headers["Host"])
        web.agents.add(self.headers["User-Agent"])
        status, headers, body = web.replies.get(url, (404, {}, b""))
        if callable(body):
            body(self)
            return
        self.send_response(status)
        for name, value in headers.items():
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    do_GET = do_HEAD = do_POST = _answer

    def log_message(self, *args):
        pass


class _Server(http.server.ThreadingHTTPServer):
    daemon_threads = True

    def server_bind(self):
        # HTTPServer.server_bind names the server by reverse DNS, which stalled for 30 s
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = "localhost", self.server_address[1]

    def handle_error(self, request, client_address):
        pass  # a client that gave up mid-reply breaks the pipe


class LocalWeb:
    """The local server, the replies it serves by URL, and what it was asked for.

    replies maps a URL to (status, headers, body); a body that is callable is
    handed the request handler and writes the whole reply itself.
    """

    def __init__(self):
        self.server = _Server(("127.0.0.1", 0), _Handler)
        self.server.web = self
        self.thread = threading.Thread(target=self.server.serve_forever, args=(0.05,),
                                       daemon=True)
        self.thread.start()
        self.clear()

    def clear(self):
        self.replies, self.requested, self.methods, self.hosts, self.dialed = {}, [], [], [], []
        self.agents, self.bodies = set(), []

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    def connect(self, scheme, host, family, sockaddr, timeout, cutoff):
        self.dialed.append(sockaddr[0])
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        cutoff.watch(sock)
        sock.settimeout(timeout)
        sock.connect(self.server.server_address)
        return sock


def resolver(addresses=None):
    """A stand-in for socket.getaddrinfo: PUBLIC_IP, or the addresses given for a host."""
    looked_up = []

    def getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
        looked_up.append(host)
        return [(socket.AF_INET6 if ":" in ip else socket.AF_INET, socket.SOCK_STREAM, 6, "",
                 (ip, port)) for ip in (addresses or {}).get(host, [PUBLIC_IP])]
    patch = mock.patch("socket.getaddrinfo", getaddrinfo)
    patch.looked_up = looked_up
    return patch


def serve_web(test, addresses=None):
    """Point the test's requests at a LocalWeb until it ends. Returns (web, the
    hosts looked up)."""
    web = LocalWeb()
    test.addCleanup(web.close)
    test.enterContext(mock.patch.object(net, "_connect", web.connect))
    lookups = resolver(addresses)
    test.enterContext(lookups)
    return web, lookups.looked_up


def redirect(location, status=302):
    return status, {"Location": location}, b""


def _get(url, limit=10, seconds=5, **options):
    return net.get(url, limit=limit, deadline=time.monotonic() + seconds, **options)


class PublicOnlyTest(unittest.TestCase):
    """No request reaches a host off the public internet, however it is named."""

    def setUp(self):
        self.web, self.looked_up = serve_web(self, {
            "intranet.com": ["10.0.0.1"], "mixed.com": [PUBLIC_IP, "127.0.0.1"],
            "mapped.com": ["::ffff:10.0.0.1"]})

    def test_urls_naming_no_public_host_are_blocked_before_any_lookup(self):
        for url in ("http://127.0.0.1:631/x.png", "http://169.254.169.254/",
                    "https://[::1]/", "http://localhost/", "https://intranet/",
                    "https://printer.local/", "https://nas.lan/", "https://git.internal/",
                    "https://files.home/", "https://mail.corp/", "https://a.test/",
                    "https://b.example/", "https://c.invalid/", "https://d.localhost/",
                    "ftp://acme.com/", "file:///etc/passwd", "https://acme.com:99999/",
                    "not a url"):
            with self.subTest(url), self.assertRaises(net.Blocked):
                _get(url)
        self.assertEqual((self.looked_up, self.web.dialed), ([], []))

    def test_a_host_resolving_to_any_private_address_is_blocked(self):
        for host in ("intranet.com", "mixed.com", "mapped.com"):
            with self.subTest(host), self.assertRaisesRegex(net.Blocked, "a private address"):
                _get(f"https://{host}/")
        self.assertEqual(self.web.requested, [])

    def test_embedded_private_ipv4_addresses_are_not_public(self):
        for address in ("10.0.0.1", "100.64.0.1", "224.0.0.1", "0.0.0.0", "::1", "fe80::1",
                        "::ffff:127.0.0.1", "2002:c0a8:0101::1", "2001:0:4136:e378::1",
                        "fc00::1", "240.0.0.1"):
            with self.subTest(address):
                self.assertFalse(net.is_public(address))
        self.assertTrue(net.is_public(PUBLIC_IP))
        self.assertTrue(net.is_public("2606:4700:4700::1111"))

    def test_a_redirect_to_a_private_host_is_blocked(self):
        self.web.replies = {"https://acme.com/": redirect("http://192.168.1.1/")}
        with self.assertRaises(net.Blocked):
            _get("https://acme.com/")
        self.web.replies = {"https://acme.com/": redirect("https://intranet.com/")}
        with self.assertRaisesRegex(net.Blocked, "intranet.com resolves to 10.0.0.1"):
            _get("https://acme.com/")
        self.assertEqual(set(self.web.dialed), {PUBLIC_IP})

    def test_redirects_stop_after_five_hops(self):
        self.web.replies = {f"https://acme.com/{i}": redirect(f"/{i + 1}") for i in range(10)}
        with self.assertRaisesRegex(net.Failure, "more than 5 redirects") as caught:
            _get("https://acme.com/0")
        self.assertEqual(len(self.web.requested), 6)
        self.assertFalse(caught.exception.transient)

    def test_the_connection_goes_to_the_checked_address_named_by_host(self):
        answers = iter([[PUBLIC_IP], ["10.0.0.1"]])

        def rebinding(host, port, family=0, type=0, proto=0, flags=0):
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port))
                    for ip in next(answers)]
        self.web.replies = {"https://acme.com/": (200, {}, b"ok")}
        with mock.patch("socket.getaddrinfo", rebinding):
            self.assertEqual(_get("https://acme.com/").body, b"ok")
        self.assertEqual((self.web.dialed, self.web.hosts), ([PUBLIC_IP], ["acme.com"]))


class ReplyTest(unittest.TestCase):
    def setUp(self):
        self.web, _ = serve_web(self)

    def test_a_reply_carries_its_body_type_final_url_and_status(self):
        self.web.replies = {"https://acme.com/a": redirect("/b", 301),
                            "https://acme.com/b": (200, {"Content-Type": "text/html"}, b"hi")}
        reply = _get("https://acme.com/a")
        self.assertEqual(reply, net.Response(b"hi", "text/html", "https://acme.com/b", 200))

    def test_the_body_is_cut_one_byte_past_the_limit(self):
        self.web.replies = {"https://acme.com/big": (200, {}, b"x" * 100_000)}
        self.assertEqual(len(_get("https://acme.com/big", limit=1000).body), 1001)

    def test_a_redirect_left_unfollowed_points_to_its_target(self):
        self.web.replies = {"https://acme.com/a": redirect("/b")}
        reply = _get("https://acme.com/a", allow_redirects=False)
        self.assertEqual((reply.status, reply.final_url, reply.body),
                         (302, "https://acme.com/b", b""))
        self.assertEqual(self.web.requested, ["https://acme.com/a"])

    def test_head_is_sent_as_head_through_redirects(self):
        self.web.replies = {"https://acme.com/a": redirect("/b"),
                            "https://acme.com/b": (200, {}, b"body")}
        reply = _get("https://acme.com/a", method="HEAD")
        self.assertEqual((reply.body, reply.final_url), (b"", "https://acme.com/b"))
        self.assertEqual(self.web.methods, ["HEAD", "HEAD"])

    def test_statuses_other_than_2xx_fail_and_say_whether_they_may_pass(self):
        for status, transient in ((404, False), (410, False), (403, False), (429, True),
                                  (503, True)):
            self.web.replies = {"https://acme.com/": (status, {}, b"")}
            with self.subTest(status), self.assertRaises(net.Failure) as caught:
                _get("https://acme.com/")
            self.assertEqual((caught.exception.status, caught.exception.transient),
                             (status, transient))
            self.assertEqual(str(caught.exception), f"HTTP {status}")

    def test_a_network_error_is_a_failure_that_may_pass(self):
        with (mock.patch.object(net, "_connect", side_effect=ConnectionRefusedError("refused")),
              self.assertRaises(net.Failure) as caught):
            _get("https://acme.com/")
        self.assertEqual((caught.exception.status, caught.exception.transient), (None, True))

    def test_data_is_sent_as_the_request_body(self):
        self.web.replies = {"https://acme.com/": (200, {}, b"ok")}
        reply = _get("https://acme.com/", method="POST", data=b'{"a":1}',
                     headers={"Content-Type": "application/json"})
        self.assertEqual(reply.body, b"ok")
        self.assertEqual(self.web.bodies, [b'{"a":1}'])
        self.assertEqual(self.web.methods, ["POST"])

    def test_headers_add_to_or_replace_the_user_agent(self):
        self.web.replies = {"https://acme.com/": (200, {}, b"")}
        _get("https://acme.com/")
        _get("https://acme.com/", headers={"User-Agent": "me@example.com"})
        self.assertEqual(self.web.agents,
                         {f"cairn/{__version__} (local desktop app)", "me@example.com"})


def drip(first):
    """A reply that sends `first`, then a byte every 50 ms for up to 3 s."""
    def send(handler):
        handler.wfile.write(first)
        for _ in range(60):
            try:
                handler.wfile.write(b"x")
                handler.wfile.flush()
            except OSError:
                return
            time.sleep(0.05)
    return 200, {}, send


class DeadlineTest(unittest.TestCase):
    """A server that keeps a request open by sending slowly is cut off on time."""

    def setUp(self):
        self.web, _ = serve_web(self)

    def _elapsed(self, url, seconds=5, **limits):
        start = time.monotonic()
        with self.assertRaisesRegex(net.Failure, "time") as caught:
            _get(url, limit=512 * 1024, seconds=seconds, **limits)
        self.assertTrue(caught.exception.transient)
        return time.monotonic() - start

    def test_headers_sent_a_byte_at_a_time_stop_at_the_header_limit(self):
        self.web.replies = {"https://acme.com/slow": drip(b"HTTP/1.1 200 OK\r\nX-Slow: ")}
        self.assertLess(self._elapsed("https://acme.com/slow", header_seconds=0.3,
                                      request_seconds=0.6), 0.6)

    def test_a_body_sent_a_byte_at_a_time_stops_at_the_request_limit(self):
        self.web.replies = {"https://acme.com/slow": drip(
            b"HTTP/1.1 200 OK\r\nContent-Type: image/png\r\nContent-Length: 999999\r\n\r\n")}
        self.assertLess(self._elapsed("https://acme.com/slow", header_seconds=0.3,
                                      request_seconds=0.6), 1.0)

    def test_headers_cut_off_by_the_deadline_fail_for_head_too(self):
        self.web.replies = {"https://acme.com/slow": drip(b"HTTP/1.1 404 Not Found\r\nX-Slow: ")}
        for method in ("HEAD", "GET"):
            with self.subTest(method):
                self.assertLess(self._elapsed("https://acme.com/slow", seconds=0.3,
                                              method=method), 0.6)

    def test_the_deadline_alone_bounds_a_request_without_hop_limits(self):
        self.web.replies = {"https://acme.com/slow": drip(
            b"HTTP/1.1 200 OK\r\nContent-Length: 999999\r\n\r\n")}
        self.assertLess(self._elapsed("https://acme.com/slow", seconds=0.4), 0.8)

    def test_no_request_starts_past_the_callers_deadline(self):
        self.web.replies = {"https://acme.com/": (200, {}, b"")}
        with self.assertRaises(net.Failure):
            _get("https://acme.com/", seconds=-1)
        self.assertEqual(self.web.requested, [])


if __name__ == "__main__":
    unittest.main()
