"""Requests for pages and icons, each with a deadline, and whether a failure may pass."""
import http.client
import time

from cairn.core import net

# wall-clock limits on one request, from its start
HEADER_SECONDS = 5
REQUEST_SECONDS = 10
# every request made for one company or one icon, redirects and fallbacks included
ITEM_SECONDS = 15


class NoIcon(ValueError):
    """No usable icon for a domain, `transient` when every failure behind it may pass."""

    def __init__(self, message, transient):
        super().__init__(message)
        self.transient = transient


def _transient(error):
    """Whether a failure may pass on a later try, as a network or TLS error, a
    timeout, or a server answering 429 or 5xx may."""
    if isinstance(error, (NoIcon, net.Failure)):
        return error.transient
    return isinstance(error, (OSError, http.client.HTTPException))


def _may_pass(transient, deadline):
    """Whether an item may succeed on a later try, given one flag per failure saying
    whether it may pass. It may when every failure may, or when the item ran out of
    time."""
    return (bool(transient) and all(transient)) or time.monotonic() >= deadline


def _reason(error):
    return str(error) or type(error).__name__


def _get(url, limit, deadline):
    """(body, content type, final url) of a GET, each hop's headers due within
    HEADER_SECONDS and its reply within REQUEST_SECONDS, the body cut at limit + 1
    bytes."""
    reply = net.get(url, limit=limit, deadline=deadline, header_seconds=HEADER_SECONDS,
                    request_seconds=REQUEST_SECONDS)
    return reply.body, reply.content_type, reply.final_url
