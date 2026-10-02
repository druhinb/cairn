"""Fetch, filter, and dedupe new-grad postings from the configured sources."""
import string
import threading
from dataclasses import dataclass

from cairn import sources, store
from cairn.core import events, settings
from cairn.jobs import logos, places


# Postings fetched before sources had names carry the listings URL as their source.
# SimplifyJobs/Summer2026-Internships has since been renamed to Summer2027.
_RENAMED_FEEDS = {
    "https://raw.githubusercontent.com/SimplifyJobs/Summer2026-Internships/dev/.github/"
    "scripts/listings.json": "SimplifyJobs/Summer2027-Internships",
}


def _specs():
    """The enabled sources, then the enabled watchlist boards."""
    cfg = settings.get()
    specs = (sources.Source(**spec) for spec in cfg.sources + cfg.watchlist)
    return [spec for spec in specs if spec.enabled]


def _load_all():
    """(source name, rows) for every source that answered, first occurrence wins.

    `sources` comes richest schema first, so the earlier feed's `category` and other
    fields survive a sparser duplicate. A source that fails to download is left out,
    so its stored postings are not mistaken for delisted ones.
    """
    batches, keys = [], set()
    for name, rows in sources.fetch_all(_specs()):
        kept = []
        for job in rows:
            key = store.url_key(job)
            if key is not None and key in keys:
                continue
            keys.add(key)
            kept.append(job)
        seen = len(rows) - len(kept)
        note = f", {seen} already seen in an earlier source" if seen else ""
        events.emit("info", text=f"[fetch] {name}: {len(kept)} postings{note}.")
        batches.append((name, kept))
    return batches


def _rename_url_sources(specs):
    renames = dict(_RENAMED_FEEDS)
    renames.update((spec.location, spec.name) for spec in specs if spec.kind == "github")
    store.rename_sources(renames)


_ASCII_LOWER = str.maketrans(string.ascii_uppercase, string.ascii_lowercase)


def _fold(text):
    """Lowercase A-Z only, as SQLite's lower() does, so store.relevant_query agrees."""
    return text.translate(_ASCII_LOWER)


@dataclass(frozen=True)
class _Rules:
    excluded: tuple
    field_excluded: tuple
    keywords: tuple
    categories: frozenset
    degrees: frozenset
    locations: tuple
    intern_terms: tuple
    off_season_internships: bool
    wanted_terms: frozenset
    max_years: int | None
    us_only: bool


def relevance_rules(cfg):
    return _Rules(
        excluded=tuple(k.lower() for k in cfg.title_exclude),
        field_excluded=tuple(k.lower() for k in cfg.title_exclude_field),
        keywords=tuple(k.lower() for k in cfg.title_keywords),
        categories=frozenset(cfg.allowed_categories),
        degrees=frozenset(cfg.degrees_held),
        locations=tuple(places.term(l) for l in cfg.location_allow),
        intern_terms=tuple(k.lower() for k in cfg.intern_terms),
        off_season_internships=cfg.include_off_season_internships,
        wanted_terms=frozenset(t.casefold() for t in cfg.wanted_intern_terms),
        max_years=cfg.max_years_required,
        us_only=cfg.us_only,
    )


def _seniority_ok(job, title, rules):
    return not any(x in title for x in rules.excluded)


def _field_ok(job, title, rules):
    return not any(x in title for x in rules.field_excluded)


def _internship_ok(job, title, rules):
    """An internship passes only with off-season internships on and a term in
    wanted_intern_terms.

    The season comes from the feed's `terms` field. Nearly every title is a bare
    "Software Engineer Intern", and matching on the title hid 446 Fall 2026
    postings. A posting with no terms is the summer cohort and is dropped.
    """
    if not _is_internship(title, rules):
        return True
    if not rules.off_season_internships:
        return False
    terms = {_fold(str(t).strip(" ")) for t in (job.get("terms") or [])}
    return bool(terms & rules.wanted_terms)


def _is_internship(title, rules):
    return any(t in title for t in rules.intern_terms)


def is_internship(job):
    return _is_internship(_fold(job.get("title") or ""), relevance_rules(settings.get()))


def _experience_ok(job, title, rules):
    years = job.get("years_required")
    return (rules.max_years is None or years is None or years <= rules.max_years
            or _is_internship(title, rules))


def _category_ok(job, title, rules):
    # category and title keyword must both pass; docs/tuning.md lists the junk either
    # lets through alone. vanshb03/New-Grad-2026 carries no category, so a posting
    # without one passes here and rests on the title rules
    category = job.get("category")
    return not category or category in rules.categories


def _keyword_ok(job, title, rules):
    return any(k in title for k in rules.keywords)


def _degree_ok(job, title, rules):
    # `degrees` lists what the posting accepts. A non-empty list that names none of
    # yours is a graduate-only role; an empty list means unstated, so keep it.
    degrees = job.get("degrees") or []
    return not (rules.degrees and degrees and not (set(degrees) & rules.degrees))


def _location_ok(job, title, rules):
    if not rules.locations:
        return True
    locs = _fold(" ".join(places.canonical(p) for p in job.get("locations") or []))
    return any(l in locs for l in rules.locations)


def _country_ok(job, title, rules):
    return not (rules.us_only and places.abroad(job.get("locations")))


# the relevance rules in the order they apply, each (name, passes(job, title, rules))
RULES = (
    ("exclude", _seniority_ok),
    ("field exclude", _field_ok),
    ("intern term", _internship_ok),
    ("experience", _experience_ok),
    ("category", _category_ok),
    ("title keyword", _keyword_ok),
    ("degree", _degree_ok),
    ("location", _location_ok),
    ("country", _country_ok),
)


def dropped_by(job, rules=None):
    """The name of the first rule in RULES that drops an active, visible posting, or
    None when it passes them all."""
    rules = rules or relevance_rules(settings.get())
    title = _fold(job.get("title") or "")
    return next((name for name, passes in RULES if not passes(job, title, rules)), None)


# store.relevant_query is the SQL form of this rule; a change here must be made there too.
def _relevant(job, rules=None):
    if not (job.get("active") and job.get("is_visible")):
        return False
    return dropped_by(job, rules) is None


def _refresh(dry_run=False, icons=True):
    """Store every posting from every source that answered, then look up the websites
    and icons of companies that have none. Returns the stored ids.

    Legacy rows came from the GitHub feeds, so they are deactivated only when every
    enabled feed answered, since a legacy posting may belong to a feed that is down.
    Watchlist boards do not count. Each covers one company, so a board that is down
    or gone says nothing about the legacy rows.

    dry_run skips the icon lookup and the ranked-link check, which write to the
    store and serve nothing when no posting gets ranked. icons=False skips the icon
    lookup alone, for a caller that starts it later.
    """
    specs = _specs()
    _rename_url_sources(specs)
    batches = _load_all()
    stored = []
    for source, rows in batches:
        ids = store.upsert_postings(rows, source)
        store.mark_inactive_missing(source, ids)
        stored.extend(ids)
    feeds = {spec.name for spec in specs if spec.kind == "github"}
    if feeds <= {source for source, _ in batches}:
        store.mark_inactive_missing("legacy", stored)
    if not dry_run:
        if icons:
            _fetch_icons()
        _check_ranked_links()
    return stored


def _fetch_icons(on_held=lambda: None):
    """Company icons are decoration, and a failure fetching them never fails a run.
    on_held runs once the fetch holds icon_job."""
    if not settings.get().company_icons:
        return
    try:
        with logos.icon_job():
            on_held()
            logos.fetch_missing()
    except logos.IconsRunning:
        events.emit("info", text="[logos] an icon fetch is already running")
    except Exception as e:  # noqa: BLE001 - reported as a warning and the run goes on
        events.emit("warn", text=f"[logos] skipped: {type(e).__name__}: {e}")


def fetch_icons_later():
    """Start the icon lookup on a daemon thread and return the thread once the lookup
    holds icon_job or has ended. icon_job keeps it apart from every other lookup, and
    it holds no run lock, so a run that starts it never waits for the lookup itself.

    A restore waits for the icons.pid marker once the run lock is free, so the
    marker must be held before the run that started the lookup releases the lock.
    """
    holding = threading.Event()

    def look_up():
        try:
            _fetch_icons(on_held=holding.set)
        finally:
            holding.set()

    thread = threading.Thread(target=look_up, name="icons", daemon=True)
    thread.start()
    holding.wait()
    return thread


def check_links(ids=None, wait=0):
    """insights.check_links(ids=ids) under its lock, waiting up to `wait` seconds for
    a check already running. Returns its counts, or None when it was skipped or
    failed; a failure never fails a run."""
    # insights reads RULES from this module, so a top-level import would be circular
    from cairn.jobs import insights  # noqa: PLC0415
    if not insights.LINK_LOCK.acquire(timeout=wait):
        events.emit("info", text="[links] skipped: another link check is running")
        return None
    try:
        return insights.check_links(ids=ids)
    except Exception as e:  # noqa: BLE001 - reported as a warning and the run goes on
        events.emit("warn", text=f"[links] skipped: {type(e).__name__}: {e}")
        return None
    finally:
        insights.LINK_LOCK.release()


def _check_ranked_links():
    found = check_links()
    if found:
        events.emit("info", text=f"[links] checked {found['checked']} ranked, "
                                 f"{found['closed']} closed")


def fetch_new(dry_run=False, icons=True):
    """(postings, counts) for the relevant, recent and unseen postings, newest first.

    The whole feed is stored and nothing is marked seen here, so a crash in ranking
    leaves the seen set untouched and the next run retries in full, at worst listing
    a posting twice. `total` and `relevant` describe this fetch; `new` is every
    unseen relevant posting stored. icons=False leaves the icon lookup to the caller.
    """
    cfg = settings.get()
    stored = _refresh(dry_run=dry_run, icons=icons)
    new = store.new_postings(cfg, cfg.recent_days)
    counts = {"total": len(stored),
              "relevant": store.count_relevant(cfg, cfg.recent_days, stored),
              "new": len(new)}
    events.emit("info", text=f"[fetch] {counts['total']} total, {counts['relevant']} "
                             f"relevant/recent, {counts['new']} new since last run.")
    return new, counts


def fetch_watchlist():
    """(postings, fetched) for the relevant, recent and unseen postings that appeared
    on an enabled watchlist board since it was last read, newest first. fetched
    counts the postings the boards list, all of which are stored.

    A board read for the first time adds nothing, so a company just followed has its
    backlog ranked by the daily run, under its cap, and not once an hour.
    """
    cfg = settings.get()
    specs = [spec for spec in (sources.Source(**spec) for spec in cfg.watchlist)
             if spec.enabled]
    fetched, appeared = 0, set()
    for source, rows in sources.fetch_all(specs):
        known = store.source_ids(source)
        ids = store.upsert_postings(rows, source)
        store.mark_inactive_missing(source, ids)
        fetched += len(ids)
        if known:
            appeared.update(set(ids) - known)
    new = [job for job in store.new_postings(cfg, cfg.recent_days) if job["id"] in appeared]
    return new, fetched


def seed():
    """Mark the whole current backlog as seen so day one doesn't dump hundreds."""
    _refresh()
    cfg = settings.get()
    backlog = store.new_postings(cfg, cfg.recent_days)
    store.mark_seen(j["id"] for j in backlog)
    events.emit("info", text=f"[fetch] SEED: marked {len(backlog)} relevant postings as seen; "
                             "nothing ranked.")
    return len(backlog)
