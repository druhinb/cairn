"""Relevance, as SQL over the postings table.

`relevant_query` is the SQL form of fetch._relevant; a test holds the two to the
same answer on every branch of the rule.
"""
import json
import time
from types import SimpleNamespace

from cairn.core import settings
from cairn.jobs import places
from cairn.store.db import connect
from cairn.store.rows import _posting

_TITLE = "lower(coalesce(postings.title, ''))"
# fetch._relevant joins the locations with a space before matching, so this does too
_LOCATIONS = ("lower(coalesce((SELECT group_concat(value, ' ') "
              "FROM json_each(postings.locations)), ''))")
_RECENCY = "max(coalesce(postings.posted_at, 0), coalesce(postings.updated_at, 0))"


def _any_substring(column, needles):
    if not needles:
        return "0", []
    return "(" + " OR ".join(f"instr({column}, ?) > 0" for _ in needles) + ")", list(needles)


def _marks(values):
    return ", ".join("?" for _ in values)


# SQLite's lower() and trim() touch only ASCII letters and spaces, so fetch._relevant
# folds the posting side with fetch._fold for the two rules to agree on non-ASCII text.
def _relevance_rule(cfg):
    """(sql, params) true of the postings fetch._relevant keeps, active and visible aside."""
    clauses, params = [], []

    def add(sql, values=()):
        clauses.append(sql)
        params.extend(values)

    excluded, values = _any_substring(
        _TITLE, [k.lower() for k in cfg.title_exclude + cfg.title_exclude_field])
    add(f"NOT {excluded}", values)

    internship, values = _any_substring(_TITLE, [k.lower() for k in cfg.intern_terms])
    wanted = sorted({t.casefold() for t in cfg.wanted_intern_terms})
    if cfg.include_off_season_internships and wanted:
        add(f"(NOT {internship} OR EXISTS (SELECT 1 FROM json_each(postings.terms) "
            f"WHERE lower(trim(value)) IN ({_marks(wanted)})))", values + wanted)
    else:
        add(f"NOT {internship}", values)

    # rules recorded before this setting existed lack it
    most_years = getattr(cfg, "max_years_required", None)
    if most_years is not None:
        internship, values = _any_substring(_TITLE, [k.lower() for k in cfg.intern_terms])
        add(f"(postings.years_required IS NULL OR {internship} "
            f"OR postings.years_required <= ?)", values + [most_years])

    categories = sorted(set(cfg.allowed_categories))
    add(f"(coalesce(postings.category, '') = '' "
        f"OR postings.category IN ({_marks(categories)}))", categories)

    add(*_any_substring(_TITLE, [k.lower() for k in cfg.title_keywords]))

    if cfg.degrees_held:
        degrees = sorted(set(cfg.degrees_held))
        add(f"(coalesce(json_array_length(postings.degrees), 0) = 0 "
            f"OR EXISTS (SELECT 1 FROM json_each(postings.degrees) "
            f"WHERE value IN ({_marks(degrees)})))", degrees)

    if cfg.location_allow:
        add(*_any_substring(_LOCATIONS, [places.term(l) for l in cfg.location_allow]))

    return "(" + " AND ".join(clauses) + ")", params


# the settings _relevance_rule reads
_RELEVANCE_SETTINGS = ("title_exclude", "title_exclude_field", "intern_terms",
                       "wanted_intern_terms", "include_off_season_internships",
                       "allowed_categories", "title_keywords", "degrees_held",
                       "location_allow", "max_years_required")


def _relevance_rules(cfg):
    return json.dumps({name: getattr(cfg, name) for name in _RELEVANCE_SETTINGS},
                      sort_keys=True)


def _flagged_rules(conn):
    """The _relevance_rules the relevant column was computed under, or None."""
    row = conn.execute("SELECT value FROM meta WHERE key = 'relevance_rules'").fetchone()
    return row[0] if row else None


def _flag_relevance(conn, where, values=()):
    """Compute the relevant column of the postings matching where, under the rules
    meta records; with none recorded the column stays as it is."""
    rules = _flagged_rules(conn)
    if rules is None:
        return
    rule, params = _relevance_rule(SimpleNamespace(**json.loads(rules)))
    conn.execute(f"UPDATE postings SET relevant = {rule} WHERE {where}", [*params, *values])


def _sync_relevance(conn, cfg):
    """Compute the relevant column under cfg's rules: every posting's when it was
    computed under others, else that of the postings stored without one."""
    rules = _relevance_rules(cfg)
    if _flagged_rules(conn) != rules:
        with conn:
            conn.execute("INSERT OR REPLACE INTO meta VALUES ('relevance_rules', ?)",
                         (rules,))
            _flag_relevance(conn, "1")
    # an UPDATE matching no row still waits for the write lock a run may hold
    elif conn.execute("SELECT 1 FROM postings WHERE relevant IS NULL LIMIT 1").fetchone():
        with conn:
            _flag_relevance(conn, "relevant IS NULL")


def sync_relevance():
    """Recompute every posting's relevance once the settings' relevance rules change."""
    _sync_relevance(connect(), settings.base())


def relevant_query(cfg):
    """(where, params) keeping exactly the postings fetch._relevant keeps. It reads
    the relevant column when that was computed under cfg's rules, and tests the rule
    on every row otherwise."""
    if _flagged_rules(connect()) == _relevance_rules(cfg):
        return "postings.active = 1 AND postings.visible = 1 AND postings.relevant = 1", []
    rule, params = _relevance_rule(cfg)
    return f"postings.active = 1 AND postings.visible = 1 AND {rule}", params


def new_postings(cfg, recent_days):
    """Relevant, recent, unseen postings whose link is not closed, newest first, in the
    raw feed shape."""
    where, params = relevant_query(cfg)
    cutoff = time.time() - recent_days * 86400
    rows = connect().execute(
        f"SELECT * FROM postings WHERE {where} AND {_RECENCY} >= ? "
        "AND postings.id NOT IN (SELECT posting_id FROM seen) "
        "AND postings.link_status IS NOT 'closed' "
        "ORDER BY coalesce(postings.posted_at, 0) DESC, postings.id",
        params + [cutoff]).fetchall()
    return [_posting(row) for row in rows]


def active_postings_since(first_seen):
    """Active, visible postings first seen at or after first_seen, in the raw feed shape."""
    rows = connect().execute("SELECT * FROM postings WHERE active = 1 AND visible = 1 "
                             "AND first_seen_at >= ? ORDER BY id", (first_seen,))
    return [_posting(row) for row in rows]


def count_relevant(cfg, recent_days, ids):
    """How many of these postings are relevant and recent."""
    where, params = relevant_query(cfg)
    return connect().execute(
        f"SELECT count(*) FROM postings WHERE {where} AND {_RECENCY} >= ? "
        "AND postings.id IN (SELECT value FROM json_each(?))",
        params + [time.time() - recent_days * 86400, json.dumps(list(ids))]).fetchone()[0]
