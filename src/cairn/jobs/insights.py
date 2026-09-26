"""Answers drawn from the stored postings: skills gaps, closed apply links, what the
relevance filter drops, companies worth following, and how applications are going.
"""
import datetime
import re
import statistics
import threading
import time
from concurrent.futures import ThreadPoolExecutor, wait
from urllib.parse import parse_qs, urlsplit

from cairn import sources, store
from cairn.core import net, settings
from cairn.jobs import descriptions, fetch

_SKILL_ALIASES = {"k8s": "kubernetes", "golang": "go", "js": "javascript",
                  "postgres": "postgresql", "ml": "machine learning"}

_TOP_SUMMARIES = """
SELECT descriptions.keywords, max(scores.fit) AS fit
FROM postings
JOIN scores ON scores.posting_id = postings.id
JOIN descriptions ON descriptions.posting_id = postings.id
WHERE postings.active = 1
  AND (instr(descriptions.keywords, 'MET:') > 0 OR instr(descriptions.keywords, 'MISSING:') > 0)
GROUP BY coalesce(postings.group_key, postings.id)
ORDER BY fit DESC
LIMIT ?
"""


def _skill(phrase):
    folded = " ".join(phrase.casefold().split()).rstrip(".")
    return _SKILL_ALIASES.get(folded, folded)


def skills_gap(limit=100):
    """[{skill, missing_in, met_in}] over the MET and MISSING lines of the `limit`
    best-fitting active postings that have them, one per group, the skills missing
    most often first."""
    missing, met = {}, {}
    for row in store.connect().execute(_TOP_SUMMARIES, (limit,)):
        found = descriptions.gap(row["keywords"])
        for counts, phrases in ((missing, found["missing"]), (met, found["met"])):
            for skill in {_skill(phrase) for phrase in phrases}:
                counts[skill] = counts.get(skill, 0) + 1
    skills = sorted(missing.keys() | met.keys(),
                    key=lambda s: (-missing.get(s, 0), -met.get(s, 0), s))
    return [{"skill": s, "missing_in": missing.get(s, 0), "met_in": met.get(s, 0)}
            for s in skills]


LINK_WORKERS = 8
LINK_SECONDS = 8
LINK_RECHECK_DAYS = 3
# held by whichever link check runs, so a run's and the app's never overlap
LINK_LOCK = threading.Lock()
# a board path may start with a locale, as Workday's /en-US/<site> does
_LOCALE = re.compile(r"[a-z]{2}(-[a-z]{2})?", re.I)
# words in a final URL that mark a sign-in page, which says nothing about the posting
_SIGN_IN = re.compile(r"(?<![a-z])(login|signin|sign-in|sso|auth|session)(?![a-z])")


def _greenhouse_root(segments):
    return len(segments) <= 1


def _one_segment(segments):
    return len(segments) == 1


def _workday_root(segments):
    return len(segments) == 1 or (len(segments) == 2 and bool(_LOCALE.fullmatch(segments[0])))


# keyed by host, or ".suffix" for every host under it. Each check says whether a
# path's segments name the board's root, where a board sends a closed posting
_BOARD_ROOTS = {
    "boards.greenhouse.io": _greenhouse_root,
    "job-boards.greenhouse.io": _greenhouse_root,
    "jobs.lever.co": _one_segment,
    "jobs.ashbyhq.com": _one_segment,
    ".myworkdayjobs.com": _workday_root,
}


def _is_board_root(url):
    parts = urlsplit(url)
    host = parts.hostname or ""
    segments = [s for s in parts.path.split("/") if s]
    for name, is_root in _BOARD_ROOTS.items():
        if host == name or (name.startswith(".") and host.endswith(name)):
            return is_root(segments)
    return False


def _is_sign_in(url):
    parts = urlsplit(url)
    names = [name.lower() for name in parse_qs(parts.query, keep_blank_values=True)]
    return (bool(_SIGN_IN.search(f"{parts.path}?{parts.query}".lower()))
            or any(name == "next" or "redirect" in name for name in names))


def _final_url(url, deadline):
    try:
        return net.get(url, method="HEAD", limit=0, deadline=deadline).final_url
    except net.Failure as e:
        if e.status != 405:
            raise
    return net.get(url, limit=0, deadline=deadline).final_url


def link_status(url, deadline=None):
    """closed for a 404 or 410, or for a redirect to a job board's root; unknown for
    a redirect to a sign-in page or any other answer that settles nothing; open for
    any other reply; None when no answer came by the deadline, LINK_SECONDS from now by
    default."""
    deadline = deadline or time.monotonic() + LINK_SECONDS
    try:
        final = _final_url(url, deadline)
    except net.Blocked:
        return "unknown"
    except net.Failure as e:
        if e.status in (404, 410):
            return "closed"
        return None if e.status is None and e.transient else "unknown"
    if final == url:
        return "open"
    if _is_sign_in(final):
        return "unknown"
    return "closed" if _is_board_root(final) and not _is_board_root(url) else "open"


def check_links(limit=60, budget_seconds=30, ids=None):
    """Check the apply links of up to limit active, relevant postings not checked in
    LINK_RECHECK_DAYS, newest first: the ranked ones, or with ids those among ids.
    Returns {checked, closed, closed_ids}.

    No request outlives budget_seconds. A link that got no answer in time is left
    as it was and tried again next time; one whose answer settles nothing keeps its
    status. Callers hold LINK_LOCK.
    """
    since = datetime.datetime.now() - datetime.timedelta(days=LINK_RECHECK_DAYS)
    due = store.links_to_check(settings.get(), limit, since.isoformat(timespec="seconds"),
                               ids)
    if not due:
        return {"checked": 0, "closed": 0, "closed_ids": []}
    deadline = time.monotonic() + budget_seconds

    def check(url):
        return link_status(url, min(deadline, time.monotonic() + LINK_SECONDS))

    pool = ThreadPoolExecutor(LINK_WORKERS)
    futures = {pool.submit(check, row["url"]): row["id"] for row in due}
    try:
        wait(futures, timeout=budget_seconds)
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    outcomes = [(posting_id, future.result()) for future, posting_id in futures.items()
                if future.done() and not future.cancelled() and future.result()]
    store.save_link_checks(outcomes)
    closed = [posting_id for posting_id, status in outcomes if status == "closed"]
    return {"checked": len(outcomes), "closed": len(closed), "closed_ids": closed}


def why_nothing(cfg, days):
    """[{rule, dropped}] for the active, visible postings first seen in the last
    `days` days: how many each relevance rule drops, in the order fetch applies
    them, each counting only what the earlier ones kept. recent, last, counts the
    postings every rule kept that are older than cfg.recent_days."""
    since = datetime.datetime.now() - datetime.timedelta(days=days)
    rules = fetch.relevance_rules(cfg)
    cutoff = time.time() - cfg.recent_days * 86400
    dropped = {name: 0 for name, _ in fetch.RULES}
    dropped["recent"] = 0
    for job in store.active_postings_since(since.isoformat(timespec="seconds")):
        rule = fetch.dropped_by(job, rules)
        if rule is None and max(job["date_posted"] or 0, job["date_updated"] or 0) < cutoff:
            rule = "recent"
        if rule is not None:
            dropped[rule] += 1
    return [{"rule": rule, "dropped": n} for rule, n in dropped.items()]


SUGGEST_FIT = 80
SUGGEST_POSTINGS = 3
SUGGEST_DAYS = 60

_STRONG_POSTINGS = """
SELECT name_key(postings.company) AS company_key, postings.company, scores.fit,
       scores.tier, coalesce(postings.group_key, postings.id) AS posting_group
FROM postings
JOIN scores ON scores.posting_id = postings.id
WHERE scores.fit >= ? AND name_key(postings.company) != ''
  AND max(coalesce(postings.posted_at, 0), coalesce(postings.updated_at, 0)) >= ?
"""


def _board_keys(watchlist):
    """(name_keys of enabled boards, name_keys of disabled ones), by company and slug."""
    enabled, disabled = set(), set()
    for spec in watchlist:
        board = sources.Source(**spec)
        slug = board.location.split(".")[0] if board.kind == "workday" else board.location
        (enabled if board.enabled else disabled).update(
            {store.name_key(board.company), store.name_key(slug)})
    return enabled, disabled


def suggested_companies(limit=8):
    """[{company, postings, best_fit, mean_tier, followed}] for up to limit companies
    with SUGGEST_POSTINGS or more postings at fit SUGGEST_FIT or above in the last
    SUGGEST_DAYS days and no enabled watchlist board. followed marks a company whose
    board is in the watchlist but disabled. A group of postings counts once."""
    enabled, disabled = _board_keys(settings.get().watchlist)
    since = time.time() - SUGGEST_DAYS * 86400
    companies = {}
    for row in store.connect().execute(_STRONG_POSTINGS, (SUGGEST_FIT, since)):
        if row["company_key"] in enabled:
            continue
        company = companies.setdefault(row["company_key"],
                                       {"names": {}, "groups": {}})
        company["names"][row["company"]] = company["names"].get(row["company"], 0) + 1
        company["groups"].setdefault(row["posting_group"], (row["fit"], row["tier"]))
    found = []
    for key, company in companies.items():
        scores = list(company["groups"].values())
        if len(scores) < SUGGEST_POSTINGS:
            continue
        tiers = [tier for _, tier in scores if tier is not None]
        found.append({"company": max(company["names"], key=company["names"].get),
                      "postings": len(scores), "best_fit": max(fit for fit, _ in scores),
                      "mean_tier": round(statistics.mean(tiers)) if tiers else None,
                      "followed": key in disabled})
    found.sort(key=lambda c: (-c["postings"], -c["best_fit"], c["company"]))
    return found[:limit]


STATS_WEEKS = 12
FUNNEL = ("saved", "applied", "interviewing", "offer", "rejected", "withdrawn")


def _when(text):
    return datetime.datetime.fromisoformat(text)


def _reached(applications, history):
    """posting id -> every status it ever held, with applied for any application sent
    and interviewing for an offer."""
    reached = {row["posting_id"]: {row["status"]} for row in applications}
    for event in history:
        reached.setdefault(event["posting_id"], set()).add(event["status"])
    for row in applications:
        if row["applied_at"]:
            reached[row["posting_id"]].add("applied")
    for statuses in reached.values():
        if "offer" in statuses:
            statuses.add("interviewing")
    return reached


def _response_days(applications, history):
    """{median, p75, n}: days from applying to the first later status."""
    later = {}
    for event in history:
        later.setdefault(event["posting_id"], []).append(event)
    waits = []
    for row in applications:
        if not row["applied_at"]:
            continue
        answer = next((e for e in later.get(row["posting_id"], ())
                       if e["at"] > row["applied_at"] and e["status"] in store.SENT
                       and e["status"] != "applied"), None)
        if answer:
            waits.append((_when(answer["at"]) - _when(row["applied_at"])).total_seconds()
                         / 86400)
    if not waits:
        return {"median": None, "p75": None, "n": 0}
    p75 = statistics.quantiles(waits, n=4, method="inclusive")[2] if len(waits) > 1 else waits[0]
    return {"median": round(statistics.median(waits), 1), "p75": round(p75, 1),
            "n": len(waits)}


def _weekly(applications, history):
    """[{week, applied, interviewing, offers}] for the last STATS_WEEKS weeks, oldest
    first, each week named by its Monday."""
    today = datetime.date.today()
    first = today - datetime.timedelta(days=today.weekday(), weeks=STATS_WEEKS - 1)
    weeks = {(first + datetime.timedelta(weeks=n)).isoformat():
             {"applied": 0, "interviewing": 0, "offers": 0} for n in range(STATS_WEEKS)}

    def count(at, column):
        day = _when(at).date()
        monday = (day - datetime.timedelta(days=day.weekday())).isoformat()
        if monday in weeks:
            weeks[monday][column] += 1

    for row in applications:
        if row["applied_at"]:
            count(row["applied_at"], "applied")
    for event in history:
        if event["status"] in ("interviewing", "offer"):
            count(event["at"], "interviewing" if event["status"] == "interviewing"
                  else "offers")
    return [{"week": week, **counts} for week, counts in weeks.items()]


def _by_source(applications, reached):
    sources_seen = {}
    for row in applications:
        statuses = reached[row["posting_id"]]
        counts = sources_seen.setdefault(row["source"], {"applied": 0, "interviewing": 0,
                                                         "offers": 0})
        counts["applied"] += "applied" in statuses
        counts["interviewing"] += "interviewing" in statuses
        counts["offers"] += "offer" in statuses
    rows = [{"source": source, **counts} for source, counts in sources_seen.items()
            if counts["applied"]]
    return sorted(rows, key=lambda r: (-r["applied"], r["source"] or ""))


def stats():
    """{funnel, response_days, weekly, by_source} over every application.

    funnel counts the postings that ever held each status, where applied covers every
    application sent and interviewing every offer. response_days measures applying
    to the first later status. by_source keeps the sources with an application sent.
    """
    conn = store.connect()
    applications = conn.execute(
        "SELECT applications.posting_id, applications.status, applications.applied_at, "
        "postings.source FROM applications "
        "JOIN postings ON postings.id = applications.posting_id").fetchall()
    history = conn.execute("SELECT posting_id, status, at FROM application_events "
                           "WHERE at IS NOT NULL ORDER BY at, id").fetchall()
    reached = _reached(applications, history)
    return {"funnel": {status: sum(status in held for held in reached.values())
                       for status in FUNNEL},
            "response_days": _response_days(applications, history),
            "weekly": _weekly(applications, history),
            "by_source": _by_source(applications, reached)}
