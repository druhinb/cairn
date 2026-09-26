"""Where postings come from: GitHub listings feeds, list READMEs, Hacker News, YC,
RemoteOK, USAJOBS, public company job boards, and plain careers pages.

Every adapter returns rows in the shape store.upsert_postings takes, so the rest of
the pipeline never knows which kind of source a posting came from.
"""
import collections
import datetime
import hashlib
import html
import html.parser
import json
import queue
import re
import threading
import time
from dataclasses import dataclass
from urllib.parse import parse_qs, parse_qsl, quote, urldefrag, urlencode, urljoin, urlparse

from cairn import events, net, secrets

BOARD_KINDS = ("greenhouse", "lever", "ashby", "smartrecruiters", "workable", "bamboohr",
               "workday")
KINDS = ("github", *BOARD_KINDS, "github_readme", "hn_hiring", "yc_waas", "remoteok",
         "usajobs", "page")

FETCH_TIMEOUT = 60
RESOLVE_TIMEOUT = 10
# the largest reply read from a JSON feed, a list README, or a YC jobs page
FEED_MAX_BYTES = 2 * 1024 * 1024
# a listings.json feed carries every posting as one JSON array; SimplifyJobs' alone runs ~17k rows
LISTINGS_MAX_BYTES = 64 * 1024 * 1024
# a Greenhouse, Lever, or Ashby board is one reply with every job; openai's Ashby
# board ran 13.8 MB and palantir's Lever board 6.2 MB in September 2026
BOARD_MAX_BYTES = 64 * 1024 * 1024
# the most postings read from one paged board per fetch
MAX_JOBS = 1000
# Workday pages hold 20 jobs and take about a second each
WORKDAY_MAX_JOBS = 500
WORKDAY_WORKERS = 4
BAMBOOHR_WORKERS = 6
WORKABLE_MAX_PAGES = 20


@dataclass(frozen=True)
class Source:
    kind: str
    # the listings.json URL for github, the raw Markdown file URL for github_readme,
    # "<tenant>.<wdN>/<site>" for workday, HN_LATEST or a thread URL for hn_hiring,
    # the search keyword for usajobs, a URL for yc_waas, remoteok and page, and the
    # board slug otherwise
    location: str
    company: str | None = None
    enabled: bool = True

    def __post_init__(self):
        if self.kind in BOARD_KINDS and not self.company:
            slug = self.location.split(".")[0] if self.kind == "workday" else self.location
            object.__setattr__(self, "company", slug.title())

    @property
    def name(self):
        """The label stored in the postings.source column and shown in logs."""
        if self.kind == "github":
            owner, repo = urlparse(self.location).path.strip("/").split("/")[:2]
            return f"{owner}/{repo}"
        if self.kind == "github_readme":
            # one repo can publish several lists, and each needs its own name
            owner, repo, _, *path = urlparse(self.location).path.strip("/").split("/")
            file = "/".join(path)
            return f"{owner}/{repo}" if file == "README.md" else f"{owner}/{repo}/{file}"
        return f"{self.kind}:{self.location}"


def _web_text(url, deadline, limit=FEED_MAX_BYTES, cut=False, headers=None, method="GET",
             data=None):
    """The body of a file or page on a public host, as text, requested through net
    by the deadline. A body over limit bytes fails, or with cut is cut to limit."""
    body = net.get(url, limit=limit, deadline=deadline, headers=headers, method=method,
                   data=data).body
    if len(body) > limit and not cut:
        raise ValueError(f"{url}: reply over {limit // 1024} KiB")
    return body[:limit].decode("utf-8", errors="replace")


def _web_json(url, deadline, headers=None, limit=FEED_MAX_BYTES, method="GET", data=None):
    return json.loads(_web_text(url, deadline, limit=limit, headers=headers, method=method,
                                data=data))


def _get_json(url, timeout=FETCH_TIMEOUT, limit=FEED_MAX_BYTES):
    return _web_json(url, time.monotonic() + timeout, limit=limit)


def _post_json(url, body, timeout=FETCH_TIMEOUT):
    return _web_json(url, time.monotonic() + timeout, method="POST",
                     data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})


def _unix(iso):
    if not iso:
        return None
    moment = datetime.datetime.fromisoformat(iso)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=datetime.timezone.utc)
    return moment.timestamp()


_BOARD_API = {
    "greenhouse": "https://boards-api.greenhouse.io/v1/boards/{}/jobs?content=false",
    "lever": "https://api.lever.co/v0/postings/{}?mode=json",
    "ashby": "https://api.ashbyhq.com/posting-api/job-board/{}",
}


def _board_json(kind, slug, timeout=FETCH_TIMEOUT):
    return _get_json(_BOARD_API[kind].format(quote(slug, safe="")), timeout=timeout,
                     limit=BOARD_MAX_BYTES)


def _path_safe(posting_id):
    """The id with everything after its source prefix hashed when it holds a slash."""
    # an id travels as one URL path segment, and the server decodes %2F before routing,
    # so /api/jobs/<id> answered 404 for every id built from a link
    if "/" not in posting_id:
        return posting_id
    kind, _, rest = posting_id.partition(":")
    return f"{kind}:{hashlib.sha256(rest.encode()).hexdigest()[:24]}"


def _posting(posting_id, company, title, url, locations, posted, updated, category=None):
    return {"id": _path_safe(posting_id), "company_name": company, "title": title, "url": url,
            "locations": locations, "category": category, "terms": [], "degrees": [],
            "sponsorship": None, "active": True, "is_visible": True,
            "date_posted": posted, "date_updated": updated}


def _board_row(kind, slug, company, board_id, title, url, locations, posted, updated):
    """The posting for one board job, or None when the job lacks an id, title, or url."""
    if board_id is None or not title or not url:
        return None
    return _posting(f"{kind}:{slug}:{board_id}", company, title, url, locations, posted,
                    updated)


def github_listings(url):
    rows = _get_json(url, limit=LISTINGS_MAX_BYTES)
    # read as an empty feed, an error body such as {} would delist the whole source
    if not isinstance(rows, list):
        raise ValueError(f"expected a list of postings, got {type(rows).__name__}")
    # vanshb03's internship feed names a single season where Simplify lists terms
    return [row if "terms" in row
            else {**row, "terms": [row["season"]] if row.get("season") else []}
            for row in rows]


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
                                     f"{total} jobs at the {FETCH_TIMEOUT} s limit")
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


def _place(*parts):
    """A location string from its non-empty parts."""
    return ", ".join(part.strip() for part in parts if part and part.strip())


def _with_remote(locations, remote):
    """locations, plus "Remote" for a remote job, so location_allow can match it."""
    if remote and not any("remote" in l.lower() for l in locations):
        return [*locations, "Remote"]
    return locations


def _smartrecruiters_page(slug, offset, timeout=FETCH_TIMEOUT):
    return _get_json(f"https://api.smartrecruiters.com/v1/companies/{quote(slug, safe='')}"
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


def _workable_page(slug, token=None, timeout=FETCH_TIMEOUT):
    body = {**_WORKABLE_QUERY, "token": token} if token else _WORKABLE_QUERY
    return _post_json(f"https://apply.workable.com/api/v3/accounts/{quote(slug, safe='')}/jobs",
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


def _bamboohr_json(slug, path, timeout=FETCH_TIMEOUT):
    # the slug is a hostname label, where quoting cannot help
    if not _BAMBOOHR_SLUG.fullmatch(slug):
        raise ValueError(f"not a BambooHR subdomain: {slug!r}")
    return _get_json(f"https://{slug}.bamboohr.com/{path}", timeout=timeout)


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
                                 f"at the {FETCH_TIMEOUT} s limit")
    errors = [error for _, error in details.values() if error is not None]
    if errors:
        events.emit("warn", text=f"[fetch] bamboohr:{slug}: {len(errors)} jobs left undated, "
                                 f"their details failed to load: {errors[-1]}")
    return {job_id: date for job_id, (date, error) in details.items() if error is None}


def bamboohr(slug, company):
    deadline = time.monotonic() + FETCH_TIMEOUT
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
    deadline = time.monotonic() + FETCH_TIMEOUT

    def page_at(offset):
        page = _post_json(api, {"appliedFacets": {}, "limit": _WORKDAY_PAGE,
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


class Skipped(Exception):
    """A source that cannot be read until the user supplies something it needs."""


_TAG = re.compile(r"<[^>]*>")


def _plain(markup):
    """The text of an HTML fragment, entities decoded and whitespace collapsed."""
    return " ".join(html.unescape(_TAG.sub(" ", markup or "")).split())


def _url_key(url):
    # store imports settings, which imports this module
    from cairn import store
    return store.url_key({"url": url})


HN_LATEST = "whoishiring"
HN_SEARCH = ("https://hn.algolia.com/api/v1/search_by_date"
             "?tags=story,author_whoishiring&hitsPerPage=10")
HN_ITEM = "https://hacker-news.firebaseio.com/v0/item/{}.json"
HN_WORKERS = 8
HN_MAX_COMMENTS = 600
# whoishiring also posts the "Who wants to be hired?" and freelancer threads
_HN_TITLE = "Ask HN: Who is hiring?"
_HN_THREAD = re.compile(r"https?://news\.ycombinator\.com/item\?id=(\d+)")
_HN_ROLE = re.compile(
    r"\b(engineer|developer|programmer|scientist|analyst|researcher|intern|graduate|"
    r"associate|sde|swe|quant|architect|designer|manager|director|head of|lead|founding|"
    r"cto|devops|sre|recruiter|product|marketing|sales|operations)", re.IGNORECASE)
_HN_WORKPLACE = re.compile(r"\b(remote|onsite|on-site|hybrid|in-office|worldwide|anywhere)\b",
                           re.IGNORECASE)
_HN_REGION = re.compile(r"\b(US|USA|UK|EU|EMEA|APAC|NYC|SF|Europe|Canada)\b")
_HN_CITY = re.compile(r"[A-Z][\w.'-]*(?: [A-Z][\w.'-]*)*, [A-Z]")
_WEB_ADDRESS = re.compile(r"\(?\s*(https?://|www\.)\S*\s*\)?")


def hn_thread(location):
    """The id of the thread a news.ycombinator.com/item?id= URL names, or None."""
    match = _HN_THREAD.fullmatch(location)
    return int(match[1]) if match else None


def _hn_threads(location, now, deadline):
    """The threads to read: the one a thread URL names, else the newest monthly
    thread, plus the month before while the newest is under a week old."""
    pinned = hn_thread(location)
    if pinned is not None:
        return [pinned]
    hits = sorted((hit for hit in _web_json(HN_SEARCH, deadline)["hits"]
                   if (hit.get("title") or "").startswith(_HN_TITLE)),
                  key=lambda hit: hit["created_at_i"], reverse=True)
    if not hits:
        raise ValueError(f"no '{_HN_TITLE}' thread found")
    fresh = now - hits[0]["created_at_i"] < 7 * 86400
    return [int(hit["objectID"]) for hit in hits[:2 if fresh else 1]]


def _hn_row(thread, comment):
    """The posting a top-level comment describes, or None when its first line has no
    "|"-separated fields. The title is the first field naming a role."""
    if not comment or comment.get("deleted") or comment.get("dead"):
        return None
    first_line = _plain(re.split(r"<p>", comment.get("text") or "", maxsplit=1)[0])
    if "|" not in first_line:
        return None
    company, *fields = [field.strip(" *") for field in first_line.split("|")]
    company = _WEB_ADDRESS.sub(" ", company).strip()
    if not company:
        return None
    title, locations = None, []
    for field in fields:
        if not field or "://" in field or field.lower().startswith("www."):
            continue
        if title is None and _HN_ROLE.search(field):
            title = field
        elif (_HN_WORKPLACE.search(field) or _HN_REGION.search(field)
              or _HN_CITY.search(field)):
            locations.append(field)
    posted = comment.get("time")
    return _posting(f"hn:{thread}:{comment['id']}", company, title or "Engineer",
                    f"https://news.ycombinator.com/item?id={comment['id']}",
                    _with_remote(locations, "remote" in first_line.lower()), posted, posted)


def hn_hiring(location):
    deadline = time.monotonic() + FETCH_TIMEOUT
    comments = []
    for thread in _hn_threads(location, time.time(), deadline):
        story = _web_json(HN_ITEM.format(thread), deadline)
        comments += [(thread, kid) for kid in story.get("kids") or []]
    comments = comments[:HN_MAX_COMMENTS]
    replies = _pooled(lambda item: _web_json(HN_ITEM.format(item[1]), deadline),
                      comments, HN_WORKERS, deadline)
    late = len(comments) - len(replies)
    if late:
        events.emit("warn", text=f"[fetch] hn_hiring:{location}: {late} comments left unread "
                                 f"at the {FETCH_TIMEOUT} s limit")
    errors = [error for _, error in replies.values() if error is not None]
    if errors:
        events.emit("warn", text=f"[fetch] hn_hiring:{location}: {len(errors)} comments "
                                 f"failed to load: {errors[-1]}")
    rows = (_hn_row(thread, replies.get((thread, kid), (None, None))[0])
            for thread, kid in comments)
    return [row for row in rows if row]


_MD_IMAGE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_MD_LINK = re.compile(r"\[([^\]]*)\]\(([^)\s]+)[^)]*\)")
_HTML_HREF = re.compile(r"""<a\s[^>]*?href=["']([^"']+)["']""", re.IGNORECASE)
_TABLE_RULE = re.compile(r"\|?(\s*:?-+:?\s*\|)*\s*:?-+:?\s*\|?")
_UNESCAPED_PIPE = re.compile(r"(?<!\\)\|")
# a column's header text: the posting field it holds
_README_COLUMNS = {
    "company": "company", "role": "title", "job title": "title", "position": "title",
    "title": "title", "location": "location", "locations": "location", "date": "date",
    "date posted": "date", "posted": "date", "age": "date", "apply": "apply",
    "application": "apply", "application/link": "apply", "link": "apply", "posting": "apply",
}
# the first match wins; a heading that matches none sets no category. speedyapply
# files its software roles under "FAANG+" and "Other" beside "Quant".
_HEADING_CATEGORIES = (
    (re.compile(r"\bquant", re.IGNORECASE), "Quant"),
    (re.compile(r"\b(data|ai|ml|machine learning)\b", re.IGNORECASE), "AI/ML/Data"),
    (re.compile(r"\b(software|swe)\b|^faang\+?$|^other$", re.IGNORECASE), "Software"),
)
_AGE = re.compile(r"(\d+)\s*(mo|months?|w|wks?|weeks?|d|days?|h|hrs?|hours?)(?:\s+ago)?",
                  re.IGNORECASE)
_AGE_SECONDS = {"h": 3600, "d": 86400, "w": 7 * 86400, "mo": 30 * 86400}
_MONTH_DAY = re.compile(r"([A-Za-z]{3})[a-z]*\.? (\d{1,2})(?:, (\d{4}))?")


def _cell_text(cell):
    text = _MD_LINK.sub(r"\1", _MD_IMAGE.sub("", cell))
    return _plain(text.replace("**", "").replace("__", "")).replace("\\|", "|")


def _cell_url(cell):
    """The first link in a table cell, Markdown or <a href>, or None."""
    cell = _MD_IMAGE.sub("", cell)
    found = [match for match in (_MD_LINK.search(cell), _HTML_HREF.search(cell)) if match]
    if not found:
        return None
    first = min(found, key=lambda match: match.start())
    return html.unescape(first[2] if first.re is _MD_LINK else first[1])


def _cells(line):
    return [cell.strip() for cell in _UNESCAPED_PIPE.split(line.strip().strip("|"))]


def _markdown_tables(text):
    """(the nearest heading above, lowercased header texts, body rows of raw cells)
    for each table in a Markdown document."""
    heading, lines = None, text.splitlines()
    for i, line in enumerate(lines):
        if line.startswith("#"):
            heading = line.lstrip("#").strip()
        elif (line.lstrip().startswith("|") and i + 1 < len(lines)
              and _TABLE_RULE.fullmatch(lines[i + 1].strip())):
            body = []
            for row in lines[i + 2:]:
                if not row.lstrip().startswith("|"):
                    break
                body.append(_cells(row))
            yield heading, [_cell_text(cell).lower() for cell in _cells(line)], body


def _heading_category(heading):
    for pattern, category in _HEADING_CATEGORIES:
        if heading and pattern.search(heading):
            return category
    return None


def _listed_at(text, now):
    """(unix time, whether it was counted back from now) for a list's date cell:
    "2d", "3 days ago", "1mo", "Sep 20, 2026", or "Sep 20", read as the latest
    Sep 20 no later than tomorrow. (None, False) for anything else."""
    text = text.strip()
    age = _AGE.fullmatch(text)
    if age:
        unit = age[2].lower()
        return now - int(age[1]) * _AGE_SECONDS["mo" if unit.startswith("mo") else unit[0]], True
    day = _MONTH_DAY.fullmatch(text)
    if day is None:
        return None, False
    year = int(day[3]) if day[3] else datetime.datetime.fromtimestamp(
        now, datetime.timezone.utc).year
    try:
        moment = datetime.datetime.strptime(f"{day[1]} {day[2]} {year}", "%b %d %Y")
        if not day[3] and moment.replace(tzinfo=datetime.timezone.utc).timestamp() > now + 86400:
            moment = moment.replace(year=year - 1)
    except ValueError:
        return None, False
    return moment.replace(tzinfo=datetime.timezone.utc).timestamp(), False


def _readme_rows(text, now):
    """The postings in every table whose header names a company and a role.

    A "↳" company cell repeats the company of the row above. The link comes from
    the apply column, else from the role cell; a row with no link is skipped.
    """
    rows = []
    for heading, header, body in _markdown_tables(text):
        company, columns = None, {}
        for i, name in enumerate(header):
            if name in _README_COLUMNS:
                columns.setdefault(_README_COLUMNS[name], i)
        if "company" not in columns or "title" not in columns:
            continue
        category = _heading_category(heading)
        for cells in body:
            def cell(field):
                i = columns.get(field)
                return cells[i] if i is not None and i < len(cells) else ""
            name = _cell_text(cell("company"))
            company = company if name == "↳" else name
            title = _cell_text(cell("title"))
            url = _cell_url(cell("apply")) or _cell_url(cell("title"))
            if not (company and title and url):
                continue
            posted, relative = _listed_at(_cell_text(cell("date")), now)
            place = _cell_text(cell("location"))
            row = _posting(f"github_readme:{_url_key(url)}", company, title, url,
                           [place] if place else [], posted, posted, category)
            rows.append({**row, "date_is_relative": True} if relative else row)
    return rows


def github_readme(url):
    return _readme_rows(_web_text(url, time.monotonic() + FETCH_TIMEOUT), time.time())


YC_HOST = "www.ycombinator.com"
YC_JOBS = f"https://{YC_HOST}/jobs/role/software-engineer"
YC_WORKERS = 4
_INERTIA_PAGE = re.compile(r'data-page="([^"]*)"')
_YC_AGE = re.compile(r"(\d+|an?) (minute|hour|day|week|month|year)s?")
_YC_UNIT_SECONDS = {"minute": 60, "hour": 3600, "day": 86400, "week": 7 * 86400,
                    "month": 30 * 86400, "year": 365 * 86400}


def _inertia_props(url, deadline):
    """The props of an Inertia page, which embeds them as JSON in its data-page attribute."""
    match = _INERTIA_PAGE.search(_web_text(url, deadline))
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
    deadline = time.monotonic() + FETCH_TIMEOUT
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
    jobs = _web_json(location, time.monotonic() + FETCH_TIMEOUT)
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
    from cairn import settings
    key, email = secrets.get_key("usajobs"), settings.get().usajobs_email
    if not (key and email):
        raise Skipped("add the USAJOBS API key and usajobs_email in Settings to read USAJOBS")
    query = urlencode({"Keyword": keyword, "JobCategoryCode": ";".join(USAJOBS_SERIES),
                       "ResultsPerPage": 250})
    reply = _web_json(f"{USAJOBS_API}?{query}", time.monotonic() + FETCH_TIMEOUT,
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


PAGE_TIMEOUT = 20
PAGE_MAX_BYTES = 512 * 1024
PAGE_MAX_LINKS = 300
PAGE_MAX_TAGS = 50_000
# an anchor's parent and up to this many more ancestors are searched for its location
PAGE_CONTEXT_LEVELS = 3
# the text runs read on each side of an anchor for its location, and inside it for
# its title and places
PAGE_CONTEXT_RUNS = 25
# query parameters that name the job a careers page link points to
_PAGE_ID_PARAMS = frozenset({"id", "jobid", "job_id", "gh_jid", "req", "requisition",
                             "posting"})
_PAGE_ROLE = re.compile(r"\b(engineer|developer|scientist|analyst|researcher|intern|"
                        r"graduate|associate|sde|swe|quant)", re.IGNORECASE)
# "San Francisco, CA", "London, United Kingdom", "Austin, TX, United States"
_PAGE_PLACE = re.compile(r"[A-Z][\w.'-]*(?: [A-Z][\w.'-]*)*"
                         r"(?:, [A-Z][\w.'-]*(?: [A-Z][\w.'-]*)*){1,2}")
_REMOTE_WORD = re.compile(r"\bremote\b", re.IGNORECASE)
_VOID_TAGS = frozenset({"area", "base", "br", "col", "embed", "hr", "img", "input", "link",
                        "meta", "source", "track", "wbr"})
# how many start tags pass between two looks at the clock
_CLOCK_EVERY = 256


_PLACE_SEPARATOR = re.compile(r"\s+[|/·•]\s+|;\s*")


def _run_places(run):
    """The places a run of text lists, such as "Austin, TX | Remote", or [] when any
    part of it is not a place. Matching whole runs keeps a title such as "Manager,
    AI Platform" from reading as a city and state."""
    parts = _PLACE_SEPARATOR.split(run)
    if all(_PAGE_PLACE.fullmatch(part) or (_REMOTE_WORD.search(part) and len(part) <= 40)
           for part in parts):
        return parts
    return []


def _names_role(title):
    return 8 <= len(title) <= 120 and bool(_PAGE_ROLE.search(title))


class _Element:
    __slots__ = ("tag", "href", "start", "unplaced", "anchor_count")

    def __init__(self, tag, href, start):
        # start indexes the element's first text run in _Anchors.runs
        self.tag, self.href, self.start = tag, href, start
        # anchors below this element still without a location, and all anchors below it
        self.unplaced, self.anchor_count = [], 0


class _Cut(Exception):
    """Parsing stops early."""


def _web_link(base_url, href):
    """The http(s) URL an href on base_url leads to, less its fragment, or None."""
    href = href.strip()
    if not href or href.startswith("#"):
        return None
    link = urldefrag(urljoin(base_url, href))[0]
    return link if urlparse(link).scheme in ("http", "https") else None


class _Anchors(html.parser.HTMLParser):
    """The web link, title, and places of up to PAGE_MAX_LINKS anchors on the page at
    base_url whose title names a role. A card whose anchor wraps a title run and then place runs is split
    between them; any other anchor takes the places named in the nearest enclosing
    element that holds no other anchor.

    Every text run is kept once, in document order, and an element records where
    its runs start, so the work grows with the page. Parsing stops at PAGE_MAX_TAGS
    start tags or at the deadline, and cut_short says why.
    """

    def __init__(self, base_url, deadline):
        super().__init__(convert_charrefs=True)
        self.anchors, self.runs, self.cut_short = [], [], None
        self._base_url, self._deadline = base_url, deadline
        self._open = [_Element("", None, 0)]
        self._open_tags = collections.Counter()
        self._tags = 0

    def parse(self, text):
        try:
            self.feed(text)
            self.close()
        except _Cut as e:
            self.cut_short = str(e)
        while len(self._open) > 1:
            self._close()

    def handle_starttag(self, tag, attrs):
        self._tags += 1
        if self._tags > PAGE_MAX_TAGS:
            raise _Cut(f"over {PAGE_MAX_TAGS} tags")
        if self._tags % _CLOCK_EVERY == 0 and time.monotonic() > self._deadline:
            raise _Cut("out of time")
        if tag not in _VOID_TAGS:
            href = dict(attrs).get("href") if tag == "a" else None
            self._open.append(_Element(tag, href, len(self.runs)))
            self._open_tags[tag] += 1

    def handle_endtag(self, tag):
        # an end tag closes every element left open inside it
        if self._open_tags[tag]:
            while self._close() != tag:
                pass

    def handle_data(self, data):
        run = " ".join(data.split())
        if run and self._open[-1].tag not in ("script", "style"):
            self.runs.append(run)

    def _close(self):
        element = self._open.pop()
        self._open_tags[element.tag] -= 1
        parent = self._open[-1]
        parent.anchor_count += element.anchor_count
        if element.tag == "a":
            if element.href:
                self._add_anchor(element, parent)
            return element.tag
        if element.anchor_count == 1 and element.unplaced:
            (anchor,) = element.unplaced
            places = [place for run in self._around(element, anchor)
                      for place in _run_places(run)]
            if places:
                anchor["places"] = list(dict.fromkeys(places))
            elif anchor["levels"] < PAGE_CONTEXT_LEVELS:
                anchor["levels"] += 1
                parent.unplaced.append(anchor)
        return element.tag

    def _around(self, element, anchor):
        """The runs of element nearest its anchor, outside the anchor."""
        start, end = anchor["runs"]
        return (self.runs[max(element.start, start - PAGE_CONTEXT_RUNS):start]
                + self.runs[end:end + PAGE_CONTEXT_RUNS])

    def _add_anchor(self, element, parent):
        parent.anchor_count += 1
        end = len(self.runs)
        runs = self.runs[element.start:min(end, element.start + PAGE_CONTEXT_RUNS)]
        split = next((i for i, run in enumerate(runs) if i and _run_places(run)), len(runs))
        title = " ".join(runs[:split])
        link = _web_link(self._base_url, element.href)
        if len(self.anchors) == PAGE_MAX_LINKS or not link or not _names_role(title):
            return
        places = [place for run in runs[split:] for place in _run_places(run)]
        anchor = {"link": link, "title": title, "runs": (element.start, end),
                  "places": list(dict.fromkeys(places)), "levels": 0}
        self.anchors.append(anchor)
        if not places:
            parent.unplaced.append(anchor)


def _page_id(link):
    """page: and the link lowercased, less its trailing slash and every query
    parameter but those in _PAGE_ID_PARAMS."""
    base, _, query = link.partition("?")
    kept = [(name, value) for name, value in parse_qsl(query)
            if name.casefold() in _PAGE_ID_PARAMS]
    path = base.lower().rstrip("/")
    return f"page:{path}?{urlencode(kept)}" if kept else f"page:{path}"


def page(url, company):
    """The links on a careers page whose text names a role; each one is a posting.
    The first PAGE_MAX_BYTES of the page are read."""
    deadline = time.monotonic() + PAGE_TIMEOUT
    parser = _Anchors(url, deadline)
    parser.parse(_web_text(url, deadline, PAGE_MAX_BYTES, cut=True))
    if parser.cut_short:
        events.emit("warn", text=f"[fetch] page:{url}: read part of the page, "
                                 f"{parser.cut_short}")
    rows = {}
    for anchor in parser.anchors:
        posting_id = _page_id(anchor["link"])
        rows.setdefault(posting_id, _posting(posting_id, company, anchor["title"],
                                             anchor["link"], anchor["places"], None, None))
    return list(rows.values())


_FEEDS = {"github": github_listings, "github_readme": github_readme,
          "hn_hiring": hn_hiring, "yc_waas": yc_waas, "remoteok": remoteok,
          "usajobs": usajobs}

# each takes the location and the company name
_BOARDS = {"greenhouse": greenhouse, "lever": lever, "ashby": ashby,
           "smartrecruiters": smartrecruiters, "workable": workable, "bamboohr": bamboohr,
           "workday": workday, "page": page}


def fetch(spec):
    if spec.kind in _FEEDS:
        return _FEEDS[spec.kind](spec.location)
    return _BOARDS[spec.kind](spec.location, spec.company)


def fetch_all(specs):
    """(source name, rows) for every source that answered.

    A source that fails or is skipped is left out, so its stored postings are not
    mistaken for delisted ones.
    """
    batches = []
    for spec in specs:
        try:
            rows = fetch(spec)
        except Skipped as e:
            events.emit("warn", text=f"[fetch] skipped {spec.name}: {e}")
            continue
        except Exception as e:  # noqa: BLE001 - keep going if one source is down
            events.emit("warn", text=f"[fetch] could not load {spec.name}: {e}")
            continue
        batches.append((spec.name, rows))
    return batches


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
