"""Requests to public hosts, each with a deadline and a size limit."""
import json
import queue
import threading
import time

from cairn.core import net

FETCH_TIMEOUT = 60

# the largest reply read from a JSON feed, a list README, or a YC jobs page
FEED_MAX_BYTES = 2 * 1024 * 1024


def read_text(url, deadline, limit=FEED_MAX_BYTES, cut=False, headers=None, method="GET",
             data=None):
    """The body of a file or page on a public host, as text, requested through net
    by the deadline. A body over limit bytes fails, or with cut is cut to limit."""
    body = net.get(url, limit=limit, deadline=deadline, headers=headers, method=method,
                   data=data).body
    if len(body) > limit and not cut:
        raise ValueError(f"{url}: reply over {limit // 1024} KiB")
    return body[:limit].decode("utf-8", errors="replace")


def read_json(url, deadline, headers=None, limit=FEED_MAX_BYTES, method="GET", data=None):
    return json.loads(read_text(url, deadline, limit=limit, headers=headers, method=method,
                                data=data))


def get_json(url, timeout=FETCH_TIMEOUT, limit=FEED_MAX_BYTES):
    return read_json(url, time.monotonic() + timeout, limit=limit)


def post_json(url, body, timeout=FETCH_TIMEOUT):
    return read_json(url, time.monotonic() + timeout, method="POST",
                     data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})


def _remaining(deadline):
    return max(0, deadline - time.monotonic())


def _pooled(work, items, workers, deadline):
    """{item: (result, None) or (None, the exception)} for each item work finished
    by the deadline, `workers` at a time.

    The workers are daemon threads, and one still waiting on a request at the
    deadline is abandoned: it holds up neither this call nor the interpreter's exit.
    """
    todo, done = queue.SimpleQueue(), queue.SimpleQueue()
    for item in items:
        todo.put(item)

    def worker():
        while time.monotonic() < deadline:
            try:
                item = todo.get_nowait()
            except queue.Empty:
                return
            try:
                done.put((item, (work(item), None)))
            except Exception as e:  # noqa: BLE001 - the caller decides what a failure means
                done.put((item, (None, e)))

    for _ in range(min(workers, len(items))):
        threading.Thread(target=worker, daemon=True).start()
    finished = {}
    while len(finished) < len(items):
        try:
            item, outcome = done.get(timeout=_remaining(deadline))
        except queue.Empty:
            break
        finished[item] = outcome
    return finished
