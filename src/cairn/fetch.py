"""Fetch, filter, and dedupe new-grad postings from the configured sources."""
import string
import threading
from dataclasses import dataclass

from cairn import events, logos, settings, sources, store


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

    `sources` is ordered richest-schema-first: the earlier feed keeps its `category`
    and other fields rather than being overwritten by a sparser duplicate. A source
    that fails to download is left out entirely, so its stored postings are not
    mistaken for delisted ones.
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
    """The relevance filter's settings, lowercased and set-ified once per fetch."""
    excluded: tuple
    field_excluded: tuple
    keywords: tuple
    categories: frozenset
    degrees: frozenset
    locations: tuple
    intern_terms: tuple
    off_season_internships: bool
    wanted_terms: frozenset


def relevance_rules(cfg):
    return _Rules(
        excluded=tuple(k.lower() for k in cfg.title_exclude),
        field_excluded=tuple(k.lower() for k in cfg.title_exclude_field),
        keywords=tuple(k.lower() for k in cfg.title_keywords),
        categories=frozenset(cfg.allowed_categories),
        degrees=frozenset(cfg.degrees_held),
        locations=tuple(l.lower() for l in cfg.location_allow),
        intern_terms=tuple(k.lower() for k in cfg.intern_terms),
        off_season_internships=cfg.include_off_season_internships,
        wanted_terms=frozenset(t.casefold() for t in cfg.wanted_intern_terms),
    )


def _seniority_ok(job, title, rules):
    return not any(x in title for x in rules.excluded)


def _field_ok(job, title, rules):
    return not any(x in title for x in rules.field_excluded)


def _internship_ok(job, title, rules):
    """Internships pass only for a term you can actually take.

    The season is in the feed's `terms` field, not the title — nearly every title
    is a bare "Software Engineer Intern", so matching on the title found nothing
    and silently hid 446 Fall 2026 postings. A posting with no terms at all is the
    summer cohort by default and is dropped.
    """
    if not any(t in title for t in rules.intern_terms):
        return True
    if not rules.off_season_internships:
        return False
    terms = {_fold(str(t).strip(" ")) for t in (job.get("terms") or [])}
    return bool(terms & rules.wanted_terms)


def _category_ok(job, title, rules):
    # Both signals required: docs/tuning.md lists the junk either one lets through
    # on its own. Not every source carries a category (vanshb03/New-Grad-2026 omits
    # it), and requiring one would silently discard everything from those feeds —
    # so an absent category falls back to the title, which still has both exclude
    # lists applied to it.
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
    locs = _fold(" ".join(job.get("locations") or []))
    return any(l in locs for l in rules.locations)


# the relevance rules in the order they apply, each (name, passes(job, title, rules))
RULES = (
    ("exclude", _seniority_ok),
    ("field exclude", _field_ok),
    ("intern term", _internship_ok),
    ("category", _category_ok),
    ("title keyword", _keyword_ok),
    ("degree", _degree_ok),
    ("location", _location_ok),
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
    Watchlist boards do not count: each covers one company, and a board that is down
    or gone says nothing about the legacy rows.

    dry_run skips icon lookups and the ranked-link check: both write to the store
    and neither is worth their time when nothing here will be ranked. icons=False
    skips the icon lookup alone, for a caller that starts it later.
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

    thread = threading.Thread(target=look_up, name="icons-after-run", daemon=True)
    thread.start()
    holding.wait()
    return thread


def check_links(ids=None, wait=0):
    """insights.check_links(ids=ids) under its lock, waiting up to `wait` seconds for
    a check already running. Returns its counts, or None when it was skipped or
    failed; a failure never fails a run."""
    # insights reads RULES from this module, so a top-level import would be circular
    from cairn import insights  # noqa: PLC0415
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
    """Return (postings, counts): relevant, recent and unseen, newest first.

    The whole feed is stored, but nothing is marked seen here. A crash in ranking
    must leave the seen set untouched so the run retries in full. The failure
    mode is a duplicate listing, never a posting silently lost. `total` and
    `relevant` describe this fetch; `new` is every unseen relevant posting stored.
    icons=False leaves the icon lookup to the caller.
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


def seed():
    """Mark the whole current backlog as seen so day one doesn't dump hundreds."""
    _refresh()
    cfg = settings.get()
    backlog = store.new_postings(cfg, cfg.recent_days)
    store.mark_seen(j["id"] for j in backlog)
    events.emit("info", text=f"[fetch] SEED: marked {len(backlog)} relevant postings as seen; "
                             "nothing ranked.")
    return len(backlog)
