"""Workday career sites, read a page of 20 jobs at a time."""
import re
import time

from cairn.core import events
from cairn.sources import web
from cairn.sources.boards import _board_row
from cairn.sources.web import _pooled, _remaining

# Workday pages hold 20 jobs and take about a second each
WORKDAY_MAX_JOBS = 500
WORKDAY_WORKERS = 4


def _concurrent_pages(page_at, page_size, limit, deadline, name):
    """Every job of an offset-paged board, up to `limit`: the first page, then the
    rest WORKDAY_WORKERS at a time. Paging stops with a warning at the first page
    that failed or was not back by the deadline, keeping the pages before it.

    page_at(offset) returns (jobs, total); only the first page's total is used.
    """
    jobs, total = page_at(0)
    total = min(limit, total or 0)
    if not jobs:
        return []
    offsets = list(range(page_size, total, page_size))
    pages = _pooled(page_at, offsets, WORKDAY_WORKERS, deadline)
    for offset in offsets:
        if offset not in pages:
            events.emit("warn", text=f"[fetch] {name}: stopped after {len(jobs)} of "
                                     f"{total} jobs at the {web.FETCH_TIMEOUT} s limit")
            break
        page, error = pages[offset]
        if error is not None:
            events.emit("warn", text=f"[fetch] {name}: stopped after {len(jobs)} of "
                                     f"{total} jobs: {error}")
            break
        batch, _ = page
        if not batch:
            break
        jobs += batch
    return jobs[:limit]


_WORKDAY_LOCATION = re.compile(r"([a-z0-9-]+)\.(wd\d+)/([A-Za-z0-9_-]+)")
_WORKDAY_HOST = re.compile(r"([a-z0-9-]+)\.(wd\d+)\.myworkdayjobs\.com")
_WORKDAY_LOCALE = re.compile(r"[a-z]{2}-[A-Z]{2}")
_WORKDAY_PAGE = 20
_WORKDAY_COUNT = re.compile(r"\d+ locations", re.IGNORECASE)


def workday_board(location):
    """(host, tenant, site) of a Workday location "<tenant>.<wdN>/<site>", or None.

    The wdN data-center shard is part of the host and cannot be derived from the
    tenant, so the location carries it.
    """
    match = _WORKDAY_LOCATION.fullmatch(location)
    if match is None:
        return None
    tenant, shard, site = match.groups()
    return f"{tenant}.{shard}.myworkdayjobs.com", tenant, site


def _workday_posted(text, now):
    """Unix time for "Posted Today", "Posted Yesterday", "Posted 3 Days Ago", or
    "Posted 30+ Days Ago", which is read as 31 days. The rows carry
    date_is_relative, so a stored posting keeps the date it was first given."""
    text = (text or "").lower()
    if "today" in text:
        return now
    if "yesterday" in text:
        return now - 86400
    match = re.search(r"(\d+)(\+?) days? ago", text)
    if match is None:
        return None
    return now - (int(match[1]) + bool(match[2])) * 86400


def _workday_locations(job):
    text = (job.get("locationsText") or "").strip()
    segments = (job.get("externalPath") or "").strip("/").split("/")
    # a job in several places lists only their count; its path names the first
    if _WORKDAY_COUNT.fullmatch(text) and len(segments) == 3 and segments[0] == "job":
        text = segments[1].replace("-", " ")
    return [text] if text else []


def workday(location, company):
    board = workday_board(location)
    if board is None:
        raise ValueError(f"not a Workday board: {location!r}, expected <tenant>.<wdN>/<site>")
    host, tenant, site = board
    api = f"https://{host}/wday/cxs/{tenant}/{site}/jobs"
    deadline = time.monotonic() + web.FETCH_TIMEOUT

    def page_at(offset):
        page = web.post_json(api, {"appliedFacets": {}, "limit": _WORKDAY_PAGE,
                                   "offset": offset, "searchText": ""},
                          timeout=max(1, _remaining(deadline)))
        return page.get("jobPostings") or [], page.get("total")

    now = time.time()
    rows = {}
    for job in _concurrent_pages(page_at, _WORKDAY_PAGE, WORKDAY_MAX_JOBS, deadline,
                                 f"workday:{location}"):
        path = job.get("externalPath") or ""
        posted = _workday_posted(job.get("postedOn"), now)
        row = _board_row(
            "workday", location, company, path.rstrip("/").rsplit("/", 1)[-1] or None,
            job.get("title"), f"https://{host}/{site}{path}" if path else None,
            _workday_locations(job), posted, posted)
        # a job can move between pages fetched at once, and is kept where first seen
        if row and row["id"] not in rows:
            rows[row["id"]] = {**row, "date_is_relative": True}
    return list(rows.values())
