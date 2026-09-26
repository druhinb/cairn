"""The posting shape every adapter returns, and the helpers that build it."""
import datetime
import hashlib
import html
import html.parser
import re


def _unix(iso):
    if not iso:
        return None
    moment = datetime.datetime.fromisoformat(iso)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=datetime.timezone.utc)
    return moment.timestamp()


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


def _place(*parts):
    return ", ".join(part.strip() for part in parts if part and part.strip())


def _with_remote(locations, remote):
    """locations, plus "Remote" for a remote job, so location_allow can match it."""
    if remote and not any("remote" in l.lower() for l in locations):
        return [*locations, "Remote"]
    return locations


class Skipped(Exception):
    """A source that cannot be read until the user supplies something it needs."""


_TAG = re.compile(r"<[^>]*>")


def _plain(markup):
    return " ".join(html.unescape(_TAG.sub(" ", markup or "")).split())


def _url_key(url):
    # store imports settings, which imports this module
    from cairn import store
    return store.url_key({"url": url})
