"""Job sites with public listings: YC's Work at a Startup, RemoteOK, and USAJOBS."""
import html
import html.parser
import json
import re
import time
from urllib.parse import urlencode, urljoin, urlparse

from cairn.core import events, secrets
from cairn.sources import web
from cairn.sources.common import Skipped, _posting, _unix
from cairn.sources.web import _pooled

YC_HOST = "www.ycombinator.com"
YC_JOBS = f"https://{YC_HOST}/jobs/role/software-engineer"
YC_WORKERS = 4
_INERTIA_PAGE = re.compile(r'data-page="([^"]*)"')
_YC_AGE = re.compile(r"(\d+|an?) (minute|hour|day|week|month|year)s?")
_YC_UNIT_SECONDS = {"minute": 60, "hour": 3600, "day": 86400, "week": 7 * 86400,
                    "month": 30 * 86400, "year": 365 * 86400}


def _inertia_props(url, deadline):
    """The props of an Inertia page, which embeds them as JSON in its data-page attribute."""
    match = _INERTIA_PAGE.search(web.read_text(url, deadline))
    if match is None:
        raise ValueError(f"{url} carries no data-page JSON")
    return json.loads(html.unescape(match[1]))["props"]


def _yc_early_career(job):
    """Whether a job takes new grads: its minimum experience is unstated, "Any (new
    grads ok)", or at most one year."""
    need = (job.get("minExperience") or "").lower()
    years = re.match(r"\d+", need)
    return not need or "new grad" in need or (years is not None and int(years[0]) <= 1)


def _yc_created(text, now):
    """Unix time for a createdAt such as "11 days", "about 1 month", or "over 3 years"."""
    match = _YC_AGE.search((text or "").lower())
    if match is None:
        return None
    count = 1 if match[1] in ("a", "an") else int(match[1])
    return now - count * _YC_UNIT_SECONDS[match[2]]


def yc_waas(location):
    """The early-career jobs on a YC jobs page and on the per-city pages its sidebar
    links on www.ycombinator.com, each of which lists at most 50."""
    deadline = time.monotonic() + web.FETCH_TIMEOUT
    first = _inertia_props(location, deadline)
    links = [link for link in dict.fromkeys(urljoin(location, path)
                                            for _, path in first.get("sidebarLinks") or [])
             if urlparse(link).hostname == YC_HOST]
    pages = _pooled(lambda link: _inertia_props(link, deadline), links, YC_WORKERS, deadline)
    read = [pages[link][0] for link in links if link in pages and pages[link][1] is None]
    if len(read) < len(links):
        events.emit("warn", text=f"[fetch] yc_waas:{location}: {len(links) - len(read)} of "
                                 f"{len(links)} city pages failed to load")
    now = time.time()
    rows = {}
    for props in [first, *read]:
        for job in props.get("jobPostings") or []:
            if (job.get("id") is None or not job.get("title") or not job.get("url")
                    or not _yc_early_career(job)):
                continue
            posted = _yc_created(job.get("createdAt"), now)
            places = [p.strip() for p in (job.get("location") or "").split(" / ") if p.strip()]
            rows.setdefault(job["id"], {
                **_posting(f"yc_waas:{job['id']}", job.get("companyName"), job["title"],
                           urljoin(location, job["url"]), places, posted, posted),
                "date_is_relative": True})
    return list(rows.values())


REMOTEOK_API = "https://remoteok.com/api"
_REMOTEOK_TAGS = frozenset({"dev", "engineer", "software", "data", "backend", "frontend",
                            "front end", "full stack", "ml", "ai"})


def remoteok(location):
    jobs = web.read_json(location, time.monotonic() + web.FETCH_TIMEOUT)
    if not isinstance(jobs, list):
        raise ValueError(f"expected a list of jobs, got {type(jobs).__name__}")
    rows = []
    # the first item is the API's legal notice, which has no id
    for job in jobs:
        if not isinstance(job, dict) or not job.get("id"):
            continue
        tags = {str(tag).lower() for tag in job.get("tags") or []}
        if not tags & _REMOTEOK_TAGS or not job.get("position") or not job.get("url"):
            continue
        place = (job.get("location") or "").strip()
        posted = _unix(job.get("date"))
        rows.append(_posting(f"remoteok:{job['id']}", job.get("company"), job["position"],
                             job["url"],
                             ["Remote"] + ([place] if place and place != "Remote" else []),
                             posted, posted))
    return rows


USAJOBS_API = "https://data.usajobs.gov/api/search"
# job series: IT management, computer science, computer engineering, operations research
USAJOBS_SERIES = ("2210", "1550", "0854", "1515")


def usajobs(keyword):
    # imported here because settings imports this module at load
    from cairn.core import settings
    key, email = secrets.get_key("usajobs"), settings.get().usajobs_email
    if not (key and email):
        raise Skipped("add the USAJOBS API key and usajobs_email in Settings to read USAJOBS")
    query = urlencode({"Keyword": keyword, "JobCategoryCode": ";".join(USAJOBS_SERIES),
                       "ResultsPerPage": 250})
    reply = web.read_json(f"{USAJOBS_API}?{query}", time.monotonic() + web.FETCH_TIMEOUT,
                          headers={"Authorization-Key": key, "User-Agent": email})
    rows = []
    for item in reply["SearchResult"]["SearchResultItems"]:
        job = item.get("MatchedObjectDescriptor") or {}
        series = {category.get("Code") for category in job.get("JobCategory") or []}
        if (not series & set(USAJOBS_SERIES) or not item.get("MatchedObjectId")
                or not job.get("PositionTitle") or not job.get("PositionURI")):
            continue
        places = [where["LocationName"] for where in job.get("PositionLocation") or []
                  if where.get("LocationName")]
        posted = _unix(job.get("PublicationStartDate"))
        rows.append(_posting(f"usajobs:{item['MatchedObjectId']}", job.get("OrganizationName"),
                             job["PositionTitle"], job["PositionURI"], places, posted, posted))
    return rows
