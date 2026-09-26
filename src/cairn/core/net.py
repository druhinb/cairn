"""HTTP requests to the public internet, bounded in size and in time.

Every request made on behalf of a feed, a careers page, a company site, or an apply
link goes through get(). The host is resolved once, every address it resolves to is
checked, and the connection goes to a checked address, so a DNS answer that changes
after the check is never used and no request reaches a private network.
"""
import http.client
import ipaddress
import re
import socket
import ssl
import threading
import time
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit, urlunsplit

from cairn import __version__

USER_AGENT = f"cairn/{__version__} (local desktop app)"
MAX_REDIRECTS = 5
CHUNK_BYTES = 16 * 1024

_PORTS = {"http": 80, "https": 443}
_REDIRECTS = frozenset({301, 302, 303, 307, 308})
_HOST_NAME = re.compile(r"[a-z0-9-]+(\.[a-z0-9-]+)*\.[a-z]{2,}")
# reserved by RFC 2606 and RFC 6761, or in common use on private networks
RESERVED_TLDS = frozenset({"corp", "example", "home", "internal", "invalid", "lan", "local",
                           "localhost", "test"})


class Blocked(ValueError):
    """A URL that is malformed or off the public internet, never requested."""


class Failure(Exception):
    """A request that got no usable answer. status is the HTTP status of a reply
    other than 2xx or a followed redirect, else None. transient is set when a later
    try may succeed: a network or TLS error, a timeout, or a 429 or 5xx."""

    def __init__(self, message, status=None, transient=False):
        super().__init__(message)
        self.status = status
        self.transient = transient


@dataclass(frozen=True)
class Response:
    """body holds up to limit + 1 bytes, so a body longer than the limit shows as
    len(body) > limit. final_url is the URL that answered, or for a redirect left
    unfollowed, the URL it points to."""
    body: bytes
    content_type: str
    final_url: str
    status: int


def public_name(host):
    """Whether host is a DNS name outside the reserved and private top-level domains."""
    return bool(_HOST_NAME.fullmatch(host or "")) and host.rsplit(".", 1)[-1] not in RESERVED_TLDS


def _public_host(url):
    """(scheme, host, port) of an http(s) URL naming a public DNS name; Blocked
    for any other URL, IP addresses and private names included."""
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError as e:
        raise Blocked(f"{url[:80]}: {e}") from None
    if parts.scheme not in _PORTS:
        raise Blocked(f"{url[:80]}: not an http(s) URL")
    host = parts.hostname or ""
    if not public_name(host):
        raise Blocked(f"{host or url[:80]}: not a public host name")
    return parts.scheme, host, port or _PORTS[parts.scheme]


def is_public(address):
    """Whether an IP address, and every IPv4 address an IPv6 one embeds, is on the
    public internet."""
    ip = ipaddress.ip_address(address)
    forms = [ip]
    if ip.version == 6:
        forms += [form for form in (ip.ipv4_mapped, ip.sixtofour, *(ip.teredo or ())) if form]
    return all(form.is_global and not (form.is_private or form.is_loopback
                                       or form.is_link_local or form.is_multicast
                                       or form.is_reserved or form.is_unspecified)
               for form in forms)


def _public_address(host, port):
    """(family, socket address) for host when every address it resolves to is
    public; Blocked otherwise."""
    found = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    for *_, sockaddr in found:
        if not is_public(sockaddr[0]):
            raise Blocked(f"{host} resolves to {sockaddr[0]}, a private address")
    if not found:
        raise Blocked(f"{host}: no address")
    family, _, _, _, sockaddr = found[0]
    return family, sockaddr


class _Cutoff:
    """Shuts the watched socket down at a deadline, which ends a read blocked on it.

    A socket timeout bounds each read alone, so a server sending a byte at a time
    would hold a request open for as long as it kept sending.
    """

    def __init__(self):
        self.fired = False
        self._sock = None
        self._timer = None
        self._lock = threading.Lock()

    def watch(self, sock):
        with self._lock:
            self._sock = sock
            if self.fired:
                self._shut()

    def arm(self, deadline):
        self.cancel()
        self._timer = threading.Timer(max(0, deadline - time.monotonic()), self._fire)
        self._timer.daemon = True
        self._timer.start()

    def cancel(self):
        if self._timer is not None:
            self._timer.cancel()

    def _fire(self):
        with self._lock:
            self.fired = True
            self._shut()

    def _shut(self):
        if self._sock is None:
            return
        try:
            # SSLSocket.shutdown drops the TLS state that a read blocked in another
            # thread is still using; the base method only shuts the file descriptor
            socket.socket.shutdown(self._sock, socket.SHUT_RDWR)
        except OSError:
            pass  # closed already, or not yet connected, where the timeout applies


def _connect(scheme, host, family, sockaddr, timeout, cutoff):
    """A socket connected to sockaddr, speaking TLS verified for host when scheme
    is https."""
    sock = socket.socket(family, socket.SOCK_STREAM)
    try:
        cutoff.watch(sock)
        sock.settimeout(timeout)
        sock.connect(sockaddr)
        if scheme == "https":
            sock = ssl.create_default_context().wrap_socket(sock, server_hostname=host)
            cutoff.watch(sock)
    except BaseException:
        sock.close()
        raise
    return sock


def _body(response, limit, deadline, cutoff):
    """Up to limit + 1 bytes of a response body, read a chunk at a time."""
    chunks, size = [], 0
    while size <= limit:
        chunk = response.read1(CHUNK_BYTES)
        if not chunk:
            break
        chunks.append(chunk)
        size += len(chunk)
        if time.monotonic() > deadline:
            raise TimeoutError("ran out of time reading the body")
    if cutoff.fired:
        raise TimeoutError("ran out of time reading the body")
    return b"".join(chunks)[:limit + 1]


class _StatusError(ValueError):
    def __init__(self, status):
        super().__init__(f"HTTP {status}")
        self.status = status


def _request(url, method, headers, data, limit, deadline, header_seconds, request_seconds):
    """(status, headers, body) of one request, redirects unfollowed. The body is
    read for a 2xx reply to a method other than HEAD, and is empty otherwise. The
    headers must arrive within header_seconds and the body within request_seconds
    of the start, when given, and neither after `deadline`."""
    scheme, host, port = _public_host(url)
    family, sockaddr = _public_address(host, port)
    start = time.monotonic()
    headers_by = min(deadline, start + header_seconds) if header_seconds else deadline
    body_by = min(deadline, start + request_seconds) if request_seconds else deadline
    if headers_by <= start:
        raise TimeoutError("out of time before the request")
    parts = urlsplit(url)
    cutoff = _Cutoff()
    cutoff.arm(headers_by)
    conn, response = http.client.HTTPConnection(host, port), None
    try:
        conn.sock = _connect(scheme, host, family, sockaddr, headers_by - start, cutoff)
        conn.putrequest(method, urlunsplit(("", "", parts.path or "/", parts.query, "")),
                        skip_host=True)
        conn.putheader("Host", host if port == _PORTS[scheme] else f"{host}:{port}")
        for name, value in {"User-Agent": USER_AGENT, **(headers or {})}.items():
            conn.putheader(name, value)
        if data is not None:
            conn.putheader("Content-Length", str(len(data)))
        conn.endheaders(data)
        response = conn.getresponse()
        # headers the cutoff ended early parse as complete
        if cutoff.fired:
            raise TimeoutError("no reply within the time limit")
        cutoff.arm(body_by)
        body = b""
        if 200 <= response.status < 300 and method != "HEAD":
            body = _body(response, limit, body_by, cutoff)
        return response.status, response.headers, body
    except (OSError, http.client.HTTPException) as e:
        if cutoff.fired:
            raise TimeoutError("no reply within the time limit") from e
        raise
    finally:
        cutoff.cancel()
        # a reply ending in Connection: close has already detached from conn
        if response is not None:
            response.close()
        conn.close()


def _follow(url, limit, deadline, method, headers, data, allow_redirects, header_seconds,
            request_seconds):
    for _ in range(MAX_REDIRECTS + 1):
        status, reply_headers, body = _request(url, method, headers, data, limit, deadline,
                                               header_seconds, request_seconds)
        location = reply_headers.get("Location")
        if status in _REDIRECTS and location:
            url = urljoin(url, location)
            if not allow_redirects:
                return Response(b"", reply_headers.get_content_type(), url, status)
            continue
        if not 200 <= status < 300:
            raise _StatusError(status)
        return Response(body, reply_headers.get_content_type(), url, status)
    raise ValueError(f"more than {MAX_REDIRECTS} redirects")


def get(url, *, limit, deadline, method="GET", headers=None, data=None, allow_redirects=True,
        header_seconds=None, request_seconds=None):
    """The Response to a request for url on the public internet, following up to
    MAX_REDIRECTS redirects, each to a public host.

    No request outlives `deadline`, a time.monotonic() value; header_seconds and
    request_seconds, when given, bound each hop's headers and whole reply from its
    start. headers add to or replace the User-Agent. data, when given, is sent as
    the request body, on every hop a redirect leads to. Raises Blocked for a URL
    off the public internet, however a redirect reached it, and Failure for any
    other request that got no 2xx reply.
    """
    try:
        return _follow(url, limit, deadline, method, headers, data, allow_redirects,
                       header_seconds, request_seconds)
    except Blocked:
        raise
    except _StatusError as e:
        raise Failure(str(e), e.status, e.status == 429 or e.status >= 500) from e
    except (OSError, http.client.HTTPException) as e:
        raise Failure(str(e) or type(e).__name__, transient=True) from e
    except ValueError as e:
        raise Failure(str(e) or type(e).__name__) from e
