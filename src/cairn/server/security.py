"""Who gets an answer. LocalOnly answers only this computer, addressed by a loopback
name, from the app's own origin. Every API request needs the session cookie the
launch URL's token trades for, and a change also needs the header only the app's
page sends.
"""
import hmac
import http.cookies
import ipaddress
from secrets import token_urlsafe
from urllib.parse import parse_qs, urlsplit

from fastapi.responses import HTMLResponse, JSONResponse, Response

LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "[::1]"})
# one per process; page_url() hands it to whatever launched the process
SESSION = token_urlsafe(32)
TOKEN_PARAM = "t"
# lib/api.js sends it with every request; no form, and no request another site can
# send without a CORS preflight, carries it
APP_HEADER = "x-cairn"
MUTATING = frozenset({"POST", "PUT", "PATCH", "DELETE"})
PAGES = frozenset({"/", "/index.html"})
# the long-lived event stream, and the restore that waits for the other requests
UNCOUNTED = frozenset({"/api/run/events", "/api/restore"})
SECURITY_HEADERS = (
    (b"content-security-policy", (b"default-src 'self'; "
                                  b"object-src 'none'; base-uri 'none'; form-action 'self'; "
                                  b"frame-ancestors 'none'")),
    (b"x-frame-options", b"DENY"),
    (b"x-content-type-options", b"nosniff"),
    (b"referrer-policy", b"no-referrer"))
LOCKED_PAGE = """<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><meta name="color-scheme" content="light dark">
<link rel="icon" href="/favicon.svg" type="image/svg+xml"><title>Cairn</title></head>
<body>
<h1>Open Cairn from the app</h1>
<p>This page works only inside Cairn. Open the Cairn app to continue.</p>
</body>
</html>
"""


def _error(status, message):
    return JSONResponse({"error": message}, status_code=status)


def _local_host(value):
    """Whether a Host header, or an Origin's netloc, names a loopback host, any port."""
    value = value.lower()
    if value.startswith("["):
        host, _, rest = value.partition("]")
        if rest and not rest.startswith(":"):
            return False
        host, port = host + "]", rest.removeprefix(":")
    else:
        host, _, port = value.partition(":")
    return host in LOCAL_HOSTS and (port == "" or port.isdigit())


def _loopback_client(scope):
    client = scope.get("client")
    try:
        address = client and ipaddress.ip_address(client[0])
    except ValueError:
        return False
    # Python before 3.11.10 reads ::ffff:127.0.0.1 as not loopback
    return bool(address) and (getattr(address, "ipv4_mapped", None) or address).is_loopback


def session_cookie(port):
    """The session cookie's name for the server on port. A browser keeps one cookie
    per host and name whatever the port, so each port's server needs its own name."""
    return f"cairn_session_{port}"


def _has_session(scope, headers):
    jar = http.cookies.SimpleCookie()
    try:
        jar.load(headers.get("cookie", ""))
    except http.cookies.CookieError:
        return False
    morsel = jar.get(session_cookie(scope["server"][1]))
    return morsel is not None and hmac.compare_digest(morsel.value.encode(), SESSION.encode())


def _has_launch_token(scope):
    tokens = parse_qs(scope["query_string"].decode("latin-1")).get(TOKEN_PARAM, [])
    return any(hmac.compare_digest(token.encode(), SESSION.encode()) for token in tokens)


def own_headers(url):
    """The headers that take a client in this process, the window's readiness check
    or the menu bar, past LocalOnly on the server at url."""
    return {"Cookie": f"{session_cookie(urlsplit(url).port)}={SESSION}", APP_HEADER: "1"}


def _refusal(scope, headers):
    """(status, message) when a request must not reach the app, else None."""
    if not _loopback_client(scope):
        return 403, "this server answers only requests from this computer"
    host = headers.get("host", "")
    if not _local_host(host):
        return 421, "this server answers only requests to 127.0.0.1, localhost, or [::1]"
    origin = headers.get("origin")
    # a page on another local port is same-site, and cookies ignore the port
    if (origin is not None and origin.lower() != f"http://{host.lower()}") \
            or headers.get("sec-fetch-site") in ("cross-site", "same-site"):
        return 403, "requests from other websites are refused"
    if scope["path"].startswith("/api/"):
        if not _has_session(scope, headers):
            return 403, "this page lost its link to Cairn. Close it and open Cairn again"
        if scope["method"] in MUTATING and APP_HEADER not in headers:
            return 403, "a change must come from the app's own page"
    return None


def _page_answer(scope, headers):
    """The answer to a request for the page, when it must not get the page itself: a
    launch URL trades its token for the session cookie, and a request with neither
    gets LOCKED_PAGE."""
    if _has_launch_token(scope):
        cookie = (f"{session_cookie(scope['server'][1])}={SESSION}; HttpOnly; "
                  f"SameSite=Strict; Path=/")
        return Response(status_code=303, headers={"location": "/", "set-cookie": cookie})
    if _has_session(scope, headers):
        return None
    return HTMLResponse(LOCKED_PAGE, status_code=403)


class LocalOnly:
    """ASGI middleware: refuse a request from another machine, host, or website, and
    an API request without the session; trade a launch URL's token for the session
    cookie; answer 503 to API requests while a restore runs. Every response gets
    SECURITY_HEADERS.

    A plain ASGI class, since BaseHTTPMiddleware would buffer the event stream.
    """

    def __init__(self, app, maintenance):
        self.app, self.maintenance = app, maintenance

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        path = scope["path"]
        send = _with_security_headers(send, api=path.startswith("/api/"))
        headers = {k.decode("latin-1"): v.decode("latin-1") for k, v in scope["headers"]}
        refusal = _refusal(scope, headers)
        if refusal:
            return await _error(*refusal)(scope, receive, send)
        if path in PAGES and scope["method"] in ("GET", "HEAD"):
            answer = _page_answer(scope, headers)
            if answer is not None:
                return await answer(scope, receive, send)
        if not path.startswith("/api/") or path in UNCOUNTED:
            return await self.app(scope, receive, send)
        if not self.maintenance.enter():
            return await _error(503, "Cairn is restoring a backup. Try again in a moment")(
                scope, receive, send)
        try:
            await self.app(scope, receive, send)
        finally:
            self.maintenance.leave()


def _with_security_headers(send, api):
    """send, adding each of SECURITY_HEADERS a response does not set itself, and
    Cache-Control: no-store to an API response that sets none."""
    added = (*SECURITY_HEADERS, *([(b"cache-control", b"no-store")] if api else []))

    async def sending(message):
        if message["type"] == "http.response.start":
            own = message.get("headers", [])
            named = {name.lower() for name, _ in own}
            message = {**message, "headers": [
                *own, *((name, value) for name, value in added if name not in named)]}
        await send(message)
    return sending
