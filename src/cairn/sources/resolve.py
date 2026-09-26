"""Turning a URL or a company name into a source spec."""
import time
from urllib.parse import parse_qs, urlparse

from cairn.core import net
from cairn.sources.boards import (
    _BAMBOOHR_SLUG,
    _bamboohr_json,
    _board_json,
    _smartrecruiters_page,
    _workable_page,
)
from cairn.sources.hn import hn_thread
from cairn.sources.job_sites import REMOTEOK_API, YC_JOBS
from cairn.sources.registry import Source
from cairn.sources.workday_jobs import _WORKDAY_HOST, _WORKDAY_LOCALE, workday_board

RESOLVE_TIMEOUT = 10

# host: (kind, path segments ahead of the slug)
_BOARD_URLS = {
    "boards.greenhouse.io": ("greenhouse", ()),
    "job-boards.greenhouse.io": ("greenhouse", ()),
    "boards-api.greenhouse.io": ("greenhouse", ("v1", "boards")),
    "jobs.lever.co": ("lever", ()),
    "api.lever.co": ("lever", ("v0", "postings")),
    "jobs.ashbyhq.com": ("ashby", ()),
    "api.ashbyhq.com": ("ashby", ("posting-api", "job-board")),
    "jobs.smartrecruiters.com": ("smartrecruiters", ()),
    "careers.smartrecruiters.com": ("smartrecruiters", ()),
    "api.smartrecruiters.com": ("smartrecruiters", ("v1", "companies")),
    "apply.workable.com": ("workable", ()),
}


# first path segments on a board host that name something other than a board:
# boards.greenhouse.io/embed/job_app is a job, apply.workable.com/api/... the API,
# and apply.workable.com/j/<code> a single posting
_NOT_SLUGS = {"greenhouse": ("embed",), "workable": ("api", "j")}


def _workday_location(host, segments):
    """"<tenant>.<wdN>/<site>" from a myworkdayjobs.com host and path, or None."""
    match = _WORKDAY_HOST.fullmatch(host)
    if match is None:
        return None
    if segments[:2] == ["wday", "cxs"]:
        segments = segments[3:]
    elif segments and _WORKDAY_LOCALE.fullmatch(segments[0]):
        segments = segments[1:]
    location = f"{match[1]}.{match[2]}/{segments[0]}" if segments else ""
    return location if workday_board(location) and segments[0] != "job" else None


def _bamboohr_subdomain(host, segments):
    slug = host.removesuffix(".bamboohr.com")
    return slug if _BAMBOOHR_SLUG.fullmatch(slug) and slug not in ("www", "api") else None


# host suffix: (kind, location from the host and path segments)
_BOARD_DOMAINS = {
    ".myworkdayjobs.com": ("workday", _workday_location),
    ".bamboohr.com": ("bamboohr", _bamboohr_subdomain),
}


def _board_slug(url):
    """(kind, location) for a job board URL, or None for any other URL."""
    parsed = urlparse(url if "://" in url else f"https://{url}")
    host = parsed.hostname or ""
    segments = [s for s in parsed.path.split("/") if s]
    for suffix, (kind, location_of) in _BOARD_DOMAINS.items():
        if host.endswith(suffix):
            location = location_of(host, segments)
            return (kind, location) if location else None
    if host not in _BOARD_URLS:
        return None
    kind, prefix = _BOARD_URLS[host]
    if kind == "greenhouse" and segments == ["embed", "job_board"]:
        slug = parse_qs(parsed.query).get("for", [None])[0]
        return (kind, slug) if slug else None
    if tuple(segments[:len(prefix)]) != prefix or len(segments) <= len(prefix):
        return None
    slug = segments[len(prefix)]
    return None if slug in _NOT_SLUGS.get(kind, ()) else (kind, slug)


def _exists(url):
    """Whether a URL answers a HEAD request; False on a 404, and other failures raise."""
    try:
        net.get(url, limit=0, deadline=time.monotonic() + RESOLVE_TIMEOUT, method="HEAD")
    except net.Failure as e:
        if e.status == 404:
            return False
        raise
    return True


_GITHUB_FILE_KINDS = {".json": "github", ".md": "github_readme"}


def _github_source(parsed):
    """(kind, location) for a GitHub file, or for a repo: its listings.json feed
    when it has one, else its README."""
    segments = [s for s in parsed.path.split("/") if s]
    if parsed.hostname == "raw.githubusercontent.com" and len(segments) >= 4:
        raw = "https://raw.githubusercontent.com/" + "/".join(segments)
    elif len(segments) >= 5 and segments[2] == "blob":
        raw = "https://raw.githubusercontent.com/" + "/".join(segments[:2] + segments[3:])
    elif len(segments) >= 2 and parsed.hostname != "raw.githubusercontent.com":
        repo = f"https://raw.githubusercontent.com/{segments[0]}/{segments[1]}/HEAD/"
        listings = repo + ".github/scripts/listings.json"
        return ("github", listings) if _exists(listings) else ("github_readme",
                                                                repo + "README.md")
    else:
        return None
    for suffix, kind in _GITHUB_FILE_KINDS.items():
        if raw.lower().endswith(suffix):
            return kind, raw
    return None


def _hn_source(parsed):
    thread = parse_qs(parsed.query).get("id", [""])[0]
    location = f"https://news.ycombinator.com/item?id={thread}"
    return ("hn_hiring", location) if parsed.path == "/item" and hn_thread(location) else None


def _yc_source(parsed):
    if parsed.hostname.endswith("workatastartup.com"):
        return "yc_waas", YC_JOBS
    path = parsed.path.rstrip("/")
    return ("yc_waas", f"https://www.ycombinator.com{path}") if path.startswith("/jobs") else None


# host: (kind, location) for a URL on it, or None
_SOURCE_HOSTS = {
    "github.com": _github_source,
    "www.github.com": _github_source,
    "raw.githubusercontent.com": _github_source,
    "news.ycombinator.com": _hn_source,
    "workatastartup.com": _yc_source,
    "www.workatastartup.com": _yc_source,
    "ycombinator.com": _yc_source,
    "www.ycombinator.com": _yc_source,
    "remoteok.com": lambda parsed: ("remoteok", REMOTEOK_API),
    "www.remoteok.com": lambda parsed: ("remoteok", REMOTEOK_API),
}


def source_for_url(url, kind=None):
    """The Source a URL names: a job board, a GitHub feed or list, a Hacker News
    thread, YC's jobs pages, or RemoteOK. With kind "page", any http(s) URL is a
    careers page. None when the URL names no source, or one of another kind than
    `kind`.

    A GitHub repo URL costs one HEAD request, which checks for a listings.json feed
    and raises when GitHub cannot be reached.
    """
    text = url.strip()
    if kind == "page":
        parsed = urlparse(text)
        is_web = parsed.scheme in ("http", "https") and bool(parsed.hostname)
        return Source("page", text) if is_web else None
    parsed = urlparse(text if "://" in text else f"https://{text}")
    host = parsed.hostname or ""
    found = _SOURCE_HOSTS[host](parsed) if host in _SOURCE_HOSTS else _board_slug(text)
    if found is None or (kind is not None and found[0] != kind):
        return None
    return Source(*found)


# a board's first page of jobs, for each kind a bare company name is tried on, in
# the order resolve_company tries them; a Workday board is found only from its URL
_FIRST_PAGE = {
    "greenhouse": lambda slug, timeout: _board_json("greenhouse", slug, timeout)["jobs"],
    "lever": lambda slug, timeout: _board_json("lever", slug, timeout),
    "ashby": lambda slug, timeout: _board_json("ashby", slug, timeout)["jobs"],
    "smartrecruiters": lambda slug, timeout: _smartrecruiters_page(slug, 0, timeout)["content"],
    "workable": lambda slug, timeout: _workable_page(slug, timeout=timeout)["results"],
    "bamboohr": lambda slug, timeout: _bamboohr_json(slug, "careers/list", timeout)["result"],
}


def _has_jobs(kind, slug):
    try:
        jobs = _FIRST_PAGE[kind](slug, RESOLVE_TIMEOUT)
    except Exception:  # noqa: BLE001 - an unreachable or malformed board is not a match
        return False
    return isinstance(jobs, list) and bool(jobs)


def resolve_company(name_or_url):
    """The board Source for a company, from its board URL or by probing its name.

    A bare name is tried as a slug on Greenhouse, Lever, Ashby, SmartRecruiters,
    Workable, then BambooHR; the first board that lists at least one job wins. A
    Workday board needs its URL. Text that looks like a URL or domain is never
    probed. None when nothing matches; never raises.
    """
    text = name_or_url.strip()
    if "/" in text or "." in text:
        board = _board_slug(text)
        return Source(*board) if board else None
    slug = "".join(text.lower().split())
    if not slug:
        return None
    for kind in _FIRST_PAGE:
        if _has_jobs(kind, slug):
            return Source(kind, slug, company=text)
    return None
