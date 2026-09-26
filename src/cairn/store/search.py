"""The jobs list: search, sort, filters, and facet counts."""
import datetime
import time

from cairn.core import settings
from cairn.jobs import places
from cairn.store.db import connect
from cairn.store.postings import (_DETAIL, _DETAIL_PARAMS, _SPONSORSHIP, _add_also_on,
                                  _add_reposts)
from cairn.store.relevance import _RECENCY, _marks, relevant_query
from cairn.store.rows import _posting

_BY_SCORE = ("scores.fit IS NULL, coalesce(scores.below_floor, 0), "
             "coalesce(scores.fit, 0) + coalesce(scores.tier, 0) DESC, scores.fit DESC, "
             "postings.posted_at DESC")

# a stated USD salary with hourly and monthly pay scaled to a year of 2080 working
# hours; NULL for pay in another currency, which reads as unstated
_ANNUAL_SALARY = ("CASE WHEN descriptions.salary_currency = 'USD' THEN "
                  "coalesce(descriptions.salary_max, descriptions.salary_min) * "
                  "CASE descriptions.salary_period WHEN 'hour' THEN 2080 "
                  "WHEN 'month' THEN 12 ELSE 1 END END")

_SORTS = {
    "score": f"postings.link_status IS 'closed', {_BY_SCORE}",
    "newest": "postings.posted_at DESC",
    "company": "lower(postings.company), lower(postings.title)",
    "updated": f"applications.updated_at DESC, {_BY_SCORE}",
    "salary": f"{_ANNUAL_SALARY} IS NULL, {_ANNUAL_SALARY} DESC, {_BY_SCORE}",
}

_VALUE_FILTERS = {
    "fit_min": "scores.fit >= ?",
    "tier_min": "scores.tier >= ?",
    "location": ("EXISTS (SELECT 1 FROM json_each(postings.locations) "
                 "WHERE instr(lower(value), lower(?)) > 0)"),
    "run_id": "scores.run_id = ?",
    "sponsorship": f"{_SPONSORSHIP} = ?",
    "salary_min": f"{_ANNUAL_SALARY} >= ?",
}
_LIST_FILTERS = {"category": "postings.category", "source": "postings.source"}

# what search filters read; the application is the group's, the posting's own when
# it has one, else the most recently updated one another member holds. SQLite fills
# a bare column beside max() from the row holding the maximum. CROSS JOIN keeps the
# few applications as the outer loop, where the planner walked every grouped posting.
_FILTERED = """
FROM postings
LEFT JOIN scores ON scores.posting_id = postings.id
LEFT JOIN descriptions ON descriptions.posting_id = postings.id
LEFT JOIN applications own ON own.posting_id = postings.id
LEFT JOIN (SELECT member.group_key, held.posting_id AS holder, max(held.updated_at)
           FROM applications held CROSS JOIN postings member ON member.id = held.posting_id
           WHERE member.group_key IS NOT NULL GROUP BY member.group_key) grouped
       ON grouped.group_key = postings.group_key
LEFT JOIN applications ON applications.posting_id = coalesce(own.posting_id, grouped.holder)
"""


def _representatives(where):
    """SQL for the id of one matching posting per group: the member holding the
    group's application, else the earliest seen, so a new score never swaps it."""
    return f"""
SELECT id FROM (
    SELECT postings.id, row_number() OVER (
        PARTITION BY coalesce(postings.group_key, postings.id)
        ORDER BY applications.posting_id IS NOT postings.id,
                 postings.first_seen_at, postings.id) AS place
    {_FILTERED} WHERE {where})
WHERE place = 1"""


def _match_expression(q):
    """Each whitespace-separated token as a quoted prefix, so user input is never FTS syntax.

    Drops NUL, which FTS5 rejects even inside a quoted string.
    """
    tokens = (q or "").replace("\x00", "").split()
    return " ".join('"' + token.replace('"', '""') + '"*' for token in tokens)


def _status_clauses(status, hide_passed, column="applications.status"):
    """(clauses, params) keeping the postings whose status, read from column, is
    listed in status."""
    status = [status] if isinstance(status, str) else list(status or ())
    clauses, params, either = [], [], []
    named = [s for s in status if s != "none"]
    if named:
        either.append(f"{column} IN ({_marks(named)})")
        params.extend(named)
    if "none" in status:
        either.append(f"{column} IS NULL")
    if either:
        clauses.append("(" + " OR ".join(either) + ")")
    if hide_passed and "passed" not in status:
        clauses.append(f"{column} IS NOT 'passed'")
    return clauses, params


def _filters(q=None, relevant_only=True, fit_min=None, tier_min=None, status=None,
             hide_passed=True, category=None, location=None, source=None,
             posted_within_days=None, found_within_hours=None, active_only=True, run_id=None,
             sponsorship=None, salary_min=None):
    """(where, params) over _FILTERED for search's filters."""
    # active first: SQLite tests the terms in order, and the same test after the
    # relevance rule's substring matches made a search four times slower
    clauses, params = ["postings.active = 1"] if active_only else [], []
    if relevant_only:
        where, values = relevant_query(settings.get())
        clauses.append(where)
        params.extend(values)
    match = _match_expression(q)
    if match:
        clauses.append("postings.id IN (SELECT id FROM postings_fts WHERE postings_fts MATCH ?)")
        params.append(match)
    given = {"fit_min": fit_min, "tier_min": tier_min,
             "location": None if location is None else places.term(location),
             "run_id": run_id, "sponsorship": sponsorship, "salary_min": salary_min}
    for name, sql in _VALUE_FILTERS.items():
        if given[name] is not None:
            clauses.append(sql)
            params.append(given[name])
    for name, column in _LIST_FILTERS.items():
        values = {"category": category, "source": source}[name]
        if values is None:
            continue
        values = [values] if isinstance(values, str) else list(values)
        clauses.append(f"{column} IN ({_marks(values)})" if values else "0")
        params.extend(values)
    status_clauses, status_params = _status_clauses(status, hide_passed)
    clauses.extend(status_clauses)
    params.extend(status_params)
    if posted_within_days is not None:
        clauses.append(f"{_RECENCY} >= ?")
        params.append(time.time() - posted_within_days * 86400)
    if found_within_hours is not None:
        found_since = datetime.datetime.now() - datetime.timedelta(hours=found_within_hours)
        clauses.append("postings.found_at >= ?")
        params.append(found_since.isoformat(timespec="seconds"))
    return " AND ".join(clauses) or "1", params


def search(q=None, relevant_only=True, fit_min=None, tier_min=None, status=None,
           hide_passed=True, category=None, location=None, source=None,
           posted_within_days=None, found_within_hours=None, active_only=True, run_id=None,
           sponsorship=None, salary_min=None, sort="score", limit=50, offset=0):
    """(rows, total): one page of matching postings and the count across all pages.

    status lists STATUSES entries, and "none" for postings that have no status.
    hide_passed drops `passed` postings unless status names that status. category
    and source take one value or a list; run_id keeps the postings scored in that run.
    found_within_hours keeps the postings Cairn first stored that recently.
    sponsorship is "yes" or "no". salary_min is a yearly USD amount that the stated
    maximum, hourly pay scaled by 2080 hours, must reach; pay stated in another
    currency counts as unstated, here and for sort="salary".

    The postings of one group come back as a single row, the one _representatives
    picks, with also_on listing the others, and total counts groups. Status
    filters read the group's application, so a posting applied to through one
    source leaves the inbox with its copies.
    """
    where, params = _filters(
        q=q, relevant_only=relevant_only, fit_min=fit_min, tier_min=tier_min,
        status=status, hide_passed=hide_passed, category=category, location=location,
        source=source, posted_within_days=posted_within_days,
        found_within_hours=found_within_hours, active_only=active_only,
        run_id=run_id, sponsorship=sponsorship, salary_min=salary_min)
    chosen = _representatives(where)
    conn = connect()
    total = conn.execute(f"SELECT count(*) FROM ({chosen})", params).fetchone()[0]
    rows = conn.execute(f"{_DETAIL} WHERE postings.id IN ({chosen}) "
                        f"ORDER BY {_SORTS[sort]}, postings.id LIMIT ? OFFSET ?",
                        _DETAIL_PARAMS + params + [limit, offset]).fetchall()
    jobs = [_posting(row) for row in rows]
    _add_also_on(jobs)
    _add_reposts(jobs)
    return jobs, total


# facet: the value it counts, a column of the candidates
_FACETS = {
    "source": "candidates.source",
    "category": "coalesce(candidates.category, 'none')",
    "status": "coalesce(candidates.status, 'none')",
    "sponsorship": "coalesce(candidates.sponsorship, 'not stated')",
}


def _list_clause(column, values):
    values = [values] if isinstance(values, str) else list(values)
    return (f"{column} IN ({_marks(values)})" if values else "0"), values


def _facet_filters(source=None, category=None, status=None, hide_passed=True,
                   sponsorship=None):
    """{facet: (clauses, params)}: each facet's own search filter over the candidates."""
    own = {name: ([], []) for name in _FACETS}
    if source is not None:
        clause, values = _list_clause("candidates.source", source)
        own["source"] = ([clause], values)
    if category is not None:
        clause, values = _list_clause("candidates.category", category)
        own["category"] = ([clause], values)
    own["status"] = _status_clauses(status, hide_passed, "candidates.status")
    if sponsorship is not None:
        own["sponsorship"] = (["candidates.sponsorship = ?"], [sponsorship])
    return own


def _candidates(where):
    """SQL naming, as candidates, the postings matching where with every facet's value."""
    return f"""
candidates AS MATERIALIZED (
    SELECT coalesce(postings.group_key, postings.id) AS posting_group, postings.source,
           postings.category, applications.status, {_SPONSORSHIP} AS sponsorship
    {_FILTERED} WHERE {where})"""


def facets(source=None, category=None, status=None, hide_passed=True, sponsorship=None,
           **filters):
    """{facet: {value: groups}} under search's filters, each facet with its own
    filter left out, so a value's count is the total that choosing it would give."""
    where, params = _filters(**filters, hide_passed=False)
    own = _facet_filters(source, category, status, hide_passed, sponsorship)
    parts = []
    for name, value in _FACETS.items():
        clauses = [c for other, (cs, _) in own.items() if other != name for c in cs]
        params += [p for other, (_, ps) in own.items() if other != name for p in ps]
        parts.append(f"SELECT '{name}' AS facet, {value} AS value, "
                     f"count(DISTINCT posting_group) AS n FROM candidates "
                     f"WHERE {' AND '.join(clauses) or '1'} GROUP BY value")
    rows = connect().execute(f"WITH {_candidates(where)} SELECT * FROM ("
                             + " UNION ALL ".join(parts) + ") ORDER BY n DESC, value", params)
    counted = {name: {} for name in _FACETS}
    for row in rows:
        counted[row["facet"]][row["value"]] = row["n"]
    return counted
