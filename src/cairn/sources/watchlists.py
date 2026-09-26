"""Watchlists as files a person can share: the companies to follow and the board
each one posts on. Importing merges a file into a watchlist; the settings check
each board when the merged watchlist is saved."""
import json
from importlib import resources

from cairn.sources.registry import BOARD_KINDS

FORMAT = "cairn-watchlist"
VERSION = 1
MAX_COMPANIES = 500
STARTERS = ("big-tech", "ai-labs", "quant", "fintech-dev-tools", "startups")


class WatchlistFileError(ValueError):
    """A one-line reason a file is not a watchlist Cairn can read."""


def export(watchlist, name=None):
    """The file for the enabled entries of watchlist, as a dict."""
    return {"format": FORMAT, "version": VERSION, "name": name,
            "companies": [{"company": spec.get("company") or spec["location"],
                           "kind": spec["kind"], "location": spec["location"]}
                          for spec in watchlist if spec.get("enabled", True)]}


def _company(entry, n):
    where = f"company {n}"
    if not isinstance(entry, dict):
        raise WatchlistFileError(f"{where} should be an object")
    kind, location = entry.get("kind"), entry.get("location")
    if kind not in BOARD_KINDS:
        raise WatchlistFileError(f"{where}: kind should be one of {', '.join(BOARD_KINDS)}")
    if not isinstance(location, str) or not location.strip():
        raise WatchlistFileError(f"{where}: missing location")
    company = entry.get("company")
    spec = {"kind": kind, "location": location.strip(), "enabled": True}
    if isinstance(company, str) and company.strip():
        spec["company"] = company.strip()
    return spec


def parse(text):
    """(name, specs) from a watchlist file's text. Raises WatchlistFileError."""
    try:
        data = json.loads(text)
    except ValueError:
        raise WatchlistFileError("the file is not a Cairn watchlist") from None
    if not isinstance(data, dict) or data.get("format") != FORMAT:
        raise WatchlistFileError("the file is not a Cairn watchlist")
    if data.get("version") != VERSION:
        raise WatchlistFileError(f"the file is watchlist version {data.get('version')}, "
                                 f"and this Cairn reads version {VERSION}")
    companies = data.get("companies")
    if not isinstance(companies, list):
        raise WatchlistFileError("the file lists no companies")
    if len(companies) > MAX_COMPANIES:
        raise WatchlistFileError(f"the file lists {len(companies)} companies, "
                                 f"more than {MAX_COMPANIES}")
    name = data.get("name") if isinstance(data.get("name"), str) else None
    return name, [_company(entry, n) for n, entry in enumerate(companies, 1)]


def _board(spec):
    return spec["kind"], spec["location"].casefold()


def merge(watchlist, specs):
    """(merged watchlist, added, turned on) with specs added to watchlist. A board
    already followed is kept as it is, and turned on when it was off."""
    merged = [dict(spec) for spec in watchlist]
    at = {_board(spec): n for n, spec in enumerate(merged)}
    added = turned_on = 0
    for spec in specs:
        n = at.get(_board(spec))
        if n is None:
            at[_board(spec)] = len(merged)
            merged.append(spec)
            added += 1
        elif merged[n].get("enabled") is False:
            merged[n]["enabled"] = True
            turned_on += 1
    return merged, added, turned_on


def starter(starter_id):
    """(name, about, specs) of the packaged list starter_id. Raises KeyError for an
    id outside STARTERS."""
    if starter_id not in STARTERS:
        raise KeyError(starter_id)
    text = (resources.files("cairn.sources") / "starter" / f"{starter_id}.json").read_text(
        encoding="utf-8")
    name, specs = parse(text)
    return name, json.loads(text)["about"], specs


def starters():
    """Every packaged list, in the order setup offers them."""
    return [{"id": starter_id, "name": name, "about": about,
             "companies": [spec["company"] for spec in specs]}
            for starter_id in STARTERS for name, about, specs in [starter(starter_id)]]
