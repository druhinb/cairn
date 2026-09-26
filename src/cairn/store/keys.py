"""Keys that match one posting across feeds, and the groups those keys form."""
import json
import re
from urllib.parse import parse_qs, parse_qsl, urlsplit

from cairn.store.rows import _list


def name_key(name):
    """A company name casefolded with its whitespace collapsed, the companies key."""
    return None if name is None else " ".join(name.casefold().split())


def web_url(url):
    """url if it is an http(s) link, else None.

    Feeds are community edited and the web app renders the url as a link, so a
    javascript: or data: url must never be stored.
    """
    try:
        scheme = urlsplit(url).scheme.lower() if isinstance(url, str) else ""
    except ValueError:
        return None
    return url if scheme in ("http", "https") else None


def url_key(job):
    """Same posting across sources, identified by where it actually applies.

    Ids are assigned per repo, so the same job carries a different id in each feed.
    The apply URL is what makes two rows the same posting; tracking-only query
    strings are dropped so they do not defeat the match. Greenhouse-hosted career
    pages such as stripe.com/jobs/search name the job only in gh_jid, which is kept.
    """
    path, _, query = (web_url(job.get("url")) or "").lower().partition("?")
    url = path.rstrip("/")
    job_id = parse_qs(query).get("gh_jid")
    if url and job_id:
        return f"{url}?gh_jid={job_id[0]}"
    item = parse_qs(query).get("id")
    if url == "https://news.ycombinator.com/item" and item:
        return f"{url}?id={item[0]}"
    if url:
        return url
    company = (job.get("company_name") or "").casefold()
    title = (job.get("title") or "").casefold()
    return f"{company}\n{title}" if company or title else None


def title_key(title):
    """A title casefolded, its punctuation dropped and whitespace collapsed.

    Qualifiers stay: "Software Engineer (New Grad)" and "Software Engineer" differ.
    """
    return " ".join(re.sub(r"[^\w\s]", " ", (title or "").casefold()).split())


_REMOTE = re.compile(r"\bremote\b")


def location_key(locations):
    """A posting's places casefolded, whitespace collapsed, deduplicated and sorted,
    with every place naming remote work folded to "remote"."""
    places = set()
    for place in locations or ():
        # "#" ends a match key in a group_key, see _key_range
        folded = " ".join(str(place).casefold().replace("#", " ").split())
        if folded:
            places.add("remote" if _REMOTE.search(folded) else folded)
    return "|".join(sorted(places))


def match_key(company, title, locations):
    """What postings of one job share across sources: company, title, and places,
    folded."""
    return f"{name_key(company) or ''}\n{title_key(title)}\n{location_key(locations)}"


_REQUISITION_PARAM = re.compile(r"gh_jid|jobid|req\w*", re.IGNORECASE)
# /jobs/4716932005, /R12345, and a last path segment ending in a requisition number
# as Workday's .../Software-Engineer_REQ_110256 does
_REQUISITION_PATHS = (
    re.compile(r"/jobs/(\d{6,})(?:/|$)"),
    re.compile(r"/r(\d{5,})(?:/|$)"),
    re.compile(r"(?:^|[/_-])(?:(?:jr|req|r)[_-]?)?(\d{5,}(?:-\d{1,3})?)$"),
)


def requisition(url):
    """The requisition id an apply URL carries, lowercased with its punctuation
    dropped, or None: a gh_jid, jobId, or req... query value, or a number the path
    names the job by."""
    path, _, query = (url or "").partition("?")
    for name, value in parse_qsl(query):
        if _REQUISITION_PARAM.fullmatch(name) and value and (
                name.lower() == "jobid" or value.isdigit()):
            return re.sub(r"[^0-9a-z]", "", value.lower())
    path = path.lower().rstrip("/")
    for pattern in _REQUISITION_PATHS:
        match = pattern.search(path)
        if match:
            return re.sub(r"[^0-9a-z]", "", match[1])
    return None


def _key_range(key):
    # a group_key is its match key, "#", and an ordinal; the places that end a match
    # key hold no "#", so the range holds that key's groups and no other
    return key + "#", key + "$"


def _row_key(row):
    return match_key(row["company"], row["title"], _list(row["locations"]))


_GROUP_COLUMNS = "SELECT id, source, company, title, locations, url, first_seen_at FROM postings"


def _assign_groups(conn, rows):
    """Set group_key on rows (_GROUP_COLUMNS), which hold every posting of each match
    key they cover.

    Earliest seen first, each posting joins the first group of its key that holds
    no posting of its source and no requisition id other than its own; else it
    starts a new one. Two same-titled postings in one feed stay apart.
    """
    groups = {}
    for row in sorted(rows, key=lambda r: (r["first_seen_at"] or "", r["id"])):
        token = requisition(row["url"])
        held = groups.setdefault(_row_key(row), [])
        group = next((group for group in held
                      if row["source"] not in group["sources"]
                      and not (token and group["tokens"] - {token})), None)
        if group is None:
            group = {"ids": [], "sources": set(), "tokens": set()}
            held.append(group)
        group["ids"].append(row["id"])
        group["sources"].add(row["source"])
        if token:
            group["tokens"].add(token)
    conn.executemany("UPDATE postings SET group_key = ? WHERE id = ?", [
        (f"{key}#{n}", posting_id) for key, held in groups.items()
        for n, group in enumerate(held, 1) for posting_id in group["ids"]])


def _regroup(conn, ids):
    """Recompute the groups of every match key these postings hold or left."""
    keys = set()
    for row in conn.execute("SELECT company, title, locations, group_key FROM postings "
                            "WHERE id IN (SELECT value FROM json_each(?))", (json.dumps(ids),)):
        keys.add(_row_key(row))
        if row["group_key"]:
            keys.add(row["group_key"].rpartition("#")[0])
    members = {}
    for key in keys:
        for row in conn.execute(f"{_GROUP_COLUMNS} WHERE group_key >= ? AND group_key < ?",
                                _key_range(key)):
            members[row["id"]] = row
    for row in conn.execute(f"{_GROUP_COLUMNS} WHERE id IN (SELECT value FROM json_each(?))",
                            (json.dumps(ids),)):
        members[row["id"]] = row
    _assign_groups(conn, list(members.values()))
