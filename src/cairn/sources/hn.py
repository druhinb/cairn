"""The monthly Hacker News "Who is hiring?" thread."""
import re
import time

from cairn.core import events
from cairn.sources import web
from cairn.sources.common import _plain, _posting, _with_remote
from cairn.sources.web import _pooled

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
    hits = sorted((hit for hit in web.read_json(HN_SEARCH, deadline)["hits"]
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
    deadline = time.monotonic() + web.FETCH_TIMEOUT
    comments = []
    for thread in _hn_threads(location, time.time(), deadline):
        story = web.read_json(HN_ITEM.format(thread), deadline)
        comments += [(thread, kid) for kid in story.get("kids") or []]
    comments = comments[:HN_MAX_COMMENTS]
    replies = _pooled(lambda item: web.read_json(HN_ITEM.format(item[1]), deadline),
                      comments, HN_WORKERS, deadline)
    late = len(comments) - len(replies)
    if late:
        events.emit("warn", text=f"[fetch] hn_hiring:{location}: {late} comments left unread "
                                 f"at the {web.FETCH_TIMEOUT} s limit")
    errors = [error for _, error in replies.values() if error is not None]
    if errors:
        events.emit("warn", text=f"[fetch] hn_hiring:{location}: {len(errors)} comments "
                                 f"failed to load: {errors[-1]}")
    rows = (_hn_row(thread, replies.get((thread, kid), (None, None))[0])
            for thread, kid in comments)
    return [row for row in rows if row]
