"""Public job-board APIs: Greenhouse, Lever, Ashby, SmartRecruiters, Workable,
and BambooHR.
"""
import re
import time
from urllib.parse import quote

from cairn.core import events
from cairn.sources import web
from cairn.sources.common import _place, _posting, _unix, _with_remote
from cairn.sources.web import _pooled, _remaining

# a Greenhouse, Lever, or Ashby board is one reply with every job; openai's Ashby
# board ran 13.8 MB and palantir's Lever board 6.2 MB in September 2026
BOARD_MAX_BYTES = 64 * 1024 * 1024
# the most postings read from one paged board per fetch
MAX_JOBS = 1000

BAMBOOHR_WORKERS = 6
WORKABLE_MAX_PAGES = 20

_BOARD_API = {
    "greenhouse": "https://boards-api.greenhouse.io/v1/boards/{}/jobs?content=false",
    "lever": "https://api.lever.co/v0/postings/{}?mode=json",
    "ashby": "https://api.ashbyhq.com/posting-api/job-board/{}",
}


def _board_json(kind, slug, timeout=web.FETCH_TIMEOUT):
    return web.get_json(_BOARD_API[kind].format(quote(slug, safe="")), timeout=timeout,
                        limit=BOARD_MAX_BYTES)


def _board_row(kind, slug, company, board_id, title, url, locations, posted, updated):
    if board_id is None or not title or not url:
        return None
    return _posting(f"{kind}:{slug}:{board_id}", company, title, url, locations, posted,
                    updated)


def greenhouse(slug, company):
    rows = []
    for job in _board_json("greenhouse", slug)["jobs"]:
        name = (job.get("location") or {}).get("name")
        rows.append(_board_row("greenhouse", slug, company, job.get("id"), job.get("title"),
                               job.get("absolute_url"), [name] if name else [],
                               _unix(job.get("first_published") or job.get("updated_at")),
                               _unix(job.get("updated_at"))))
    return [row for row in rows if row]


def lever(slug, company):
    rows = []
    for job in _board_json("lever", slug):
        categories = job.get("categories") or {}
        locations = list(categories.get("allLocations")
                         or ([categories["location"]] if categories.get("location") else []))
        # location_allow matches location text, and Lever marks remote work only in
        # workplaceType
        if job.get("workplaceType") == "remote" and not any(
                "remote" in l.lower() for l in locations):
            locations.append("Remote")
        created = job["createdAt"] / 1000 if job.get("createdAt") else None
        rows.append(_board_row("lever", slug, company, job.get("id"), job.get("text"),
                               job.get("hostedUrl"), locations, created, created))
    return [row for row in rows if row]


def ashby(slug, company):
    rows = []
    for job in _board_json("ashby", slug)["jobs"]:
        if not job.get("isListed", True):
            continue
        locations = [job["location"]] if job.get("location") else []
        locations += [s["location"] for s in job.get("secondaryLocations") or []
                      if s.get("location")]
        published = _unix(job.get("publishedAt"))
        rows.append(_board_row("ashby", slug, company, job.get("id"), job.get("title"),
                               job.get("jobUrl"), locations, published, published))
    return [row for row in rows if row]


def _pages(page_at):
    """Every job of an offset-paged board, up to MAX_JOBS.

    page_at(offset) returns (jobs, total); only the first page's total is used.
    """
    jobs, total = [], MAX_JOBS
    while len(jobs) < total:
        batch, reported = page_at(len(jobs))
        if not jobs:
            total = min(MAX_JOBS, reported or 0)
        if not batch:
            break
        jobs += batch
    return jobs[:MAX_JOBS]


def _smartrecruiters_page(slug, offset, timeout=web.FETCH_TIMEOUT):
    return web.get_json(f"https://api.smartrecruiters.com/v1/companies/{quote(slug, safe='')}"
                        f"/postings?limit=100&offset={offset}", timeout=timeout)


def smartrecruiters(slug, company):
    def page_at(offset):
        page = _smartrecruiters_page(slug, offset)
        return page["content"], page.get("totalFound")

    rows = []
    for job in _pages(page_at):
        where = job.get("location") or {}
        # fullLocation leaves an empty part for a missing region: "Hyderabad, , India"
        place = (_place(*(where.get("fullLocation") or "").split(","))
                 or _place(where.get("city"), where.get("region"), where.get("country")))
        released = _unix(job.get("releasedDate"))
        # titles carry non-breaking spaces, which title_keywords would not match
        title = " ".join((job.get("name") or "").split())
        rows.append(_board_row(
            "smartrecruiters", slug, company, job.get("id"), title,
            f"https://jobs.smartrecruiters.com/{slug}/{job['id']}" if job.get("id") else None,
            _with_remote([place] if place else [], where.get("remote")), released, released))
    return [row for row in rows if row]


_WORKABLE_QUERY = {"query": "", "location": [], "department": [], "worktype": [], "remote": []}


def _workable_page(slug, token=None, timeout=web.FETCH_TIMEOUT):
    body = {**_WORKABLE_QUERY, "token": token} if token else _WORKABLE_QUERY
    return web.post_json(f"https://apply.workable.com/api/v3/accounts/{quote(slug, safe='')}/jobs",
                         body, timeout=timeout)


def workable(slug, company):
    page = _workable_page(slug)
    jobs, pages = list(page.get("results") or []), 1
    while (page.get("results") and page.get("nextPage") and pages < WORKABLE_MAX_PAGES
           and len(jobs) < MAX_JOBS):
        page = _workable_page(slug, page["nextPage"])
        jobs += page.get("results") or []
        pages += 1
    rows = []
    for job in jobs[:MAX_JOBS]:
        places = [_place(w.get("city"), w.get("region"), w.get("country"))
                  for w in job.get("locations") or [job.get("location") or {}]]
        remote = job.get("remote") or job.get("workplace") == "remote"
        published = _unix(job.get("published"))
        rows.append(_board_row(
            "workable", slug, company, job.get("id"), job.get("title"),
            f"https://apply.workable.com/{slug}/j/{job['shortcode']}/"
            if job.get("shortcode") else None,
            _with_remote([p for p in places if p], remote), published, published))
    return [row for row in rows if row]


_BAMBOOHR_SLUG = re.compile(r"[A-Za-z0-9-]+")


def _bamboohr_json(slug, path, timeout=web.FETCH_TIMEOUT):
    # the slug is a hostname label, where quoting cannot help
    if not _BAMBOOHR_SLUG.fullmatch(slug):
        raise ValueError(f"not a BambooHR subdomain: {slug!r}")
    return web.get_json(f"https://{slug}.bamboohr.com/{path}", timeout=timeout)


def _bamboohr_posted(slug, jobs, deadline):
    """{job id: unix time posted} from each job's detail, fetched BAMBOOHR_WORKERS at
    a time. A job whose detail failed or was not back by the deadline has no date."""
    def posted(job_id):
        path = f"careers/{quote(str(job_id), safe='')}/detail"
        detail = _bamboohr_json(slug, path, timeout=max(1, _remaining(deadline)))
        return _unix(detail["result"]["jobOpening"].get("datePosted"))

    ids = list(dict.fromkeys(job["id"] for job in jobs if job.get("id") is not None))
    details = _pooled(posted, ids, BAMBOOHR_WORKERS, deadline)
    late = len(ids) - len(details)
    if late:
        events.emit("warn", text=f"[fetch] bamboohr:{slug}: {late} jobs left undated "
                                 f"at the {web.FETCH_TIMEOUT} s limit")
    errors = [error for _, error in details.values() if error is not None]
    if errors:
        events.emit("warn", text=f"[fetch] bamboohr:{slug}: {len(errors)} jobs left undated, "
                                 f"their details failed to load: {errors[-1]}")
    return {job_id: date for job_id, (date, error) in details.items() if error is None}


def bamboohr(slug, company):
    deadline = time.monotonic() + web.FETCH_TIMEOUT
    jobs = _bamboohr_json(slug, "careers/list")["result"][:MAX_JOBS]
    # the list carries no dates; each job's detail has datePosted
    dates = _bamboohr_posted(slug, jobs, deadline)
    rows = []
    for job in jobs:
        where = job.get("location") or {}
        posted = dates.get(job.get("id"))
        place = _place(where.get("city"), where.get("state"))
        rows.append(_board_row(
            "bamboohr", slug, company, job.get("id"), job.get("jobOpeningName"),
            f"https://{slug}.bamboohr.com/careers/{job['id']}" if job.get("id") else None,
            _with_remote([place] if place else [], job.get("isRemote")), posted, posted))
    return [row for row in rows if row]
