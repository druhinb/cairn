"""Stored postings: upserts, lookups, and apply-link checks."""
import json

from cairn.core import ui
from cairn.jobs import places
from cairn.store import db
from cairn.store.db import connect
from cairn.store.keys import _regroup, url_key, web_url
from cairn.store.relevance import _flag_relevance, relevant_query
from cairn.store.rows import _json_list, _posting
from cairn.store.schema import SENT


def _canonical_places(locations):
    return None if locations is None else [places.canonical(p) for p in locations]


def _stored_id(conn, job, key):
    """The id a feed row is stored under: its own, or that of a row with its url."""
    if key is None or conn.execute("SELECT 1 FROM postings WHERE id = ?",
                                   (job["id"],)).fetchone():
        return job["id"]
    match = conn.execute("SELECT id FROM postings WHERE url_key = ? LIMIT 1",
                         (key,)).fetchone()
    return match["id"] if match else job["id"]


_UPSERT = """
INSERT INTO postings (id, source, company, title, url, url_key, locations, category,
                      terms, degrees, sponsorship, company_url, active, visible,
                      posted_at, updated_at, first_seen_at, last_seen_at)
VALUES (:id, :source, :company, :title, :url, :url_key, :locations, :category,
        :terms, :degrees, :sponsorship, :company_url, :active, :visible, :posted_at,
        :updated_at, :now, :now)
ON CONFLICT(id) DO UPDATE SET
    source = excluded.source, company = excluded.company, title = excluded.title,
    url = excluded.url, url_key = excluded.url_key, locations = excluded.locations,
    category = excluded.category, terms = excluded.terms, degrees = excluded.degrees,
    sponsorship = excluded.sponsorship, company_url = excluded.company_url,
    active = excluded.active, visible = excluded.visible,
    posted_at = CASE WHEN :date_is_relative THEN coalesce(posted_at, excluded.posted_at)
                     ELSE excluded.posted_at END,
    updated_at = excluded.updated_at, last_seen_at = excluded.last_seen_at
"""


def upsert_postings(rows, source):
    """Store raw feed rows from one source. Returns the ids they were stored under.

    A row whose url matches an existing posting under another id updates that
    posting, so the result is the list to hand to mark_inactive_missing. A row with
    a url that is not http(s) is skipped. A row with date_is_relative set, whose
    date_posted was counted back from "Posted 3 Days Ago" and moves with the day it
    was fetched, leaves a stored posting's posted_at as it was.
    """
    conn = connect()
    now = db.now()
    stored = []
    unsafe = 0
    with conn:
        for job in rows:
            if not job.get("id"):
                continue
            if job.get("url") and not web_url(job["url"]):
                unsafe += 1
                continue
            key = url_key(job)
            posting_id = _stored_id(conn, job, key)
            conn.execute(_UPSERT, {
                "id": posting_id, "source": source, "company": job.get("company_name"),
                "title": job.get("title"), "url": job.get("url"), "url_key": key,
                "locations": _json_list(_canonical_places(job.get("locations"))),
                "category": job.get("category"), "terms": _json_list(job.get("terms")),
                "degrees": _json_list(job.get("degrees")),
                "sponsorship": job.get("sponsorship"),
                "company_url": web_url(job.get("company_url")),
                "active": int(bool(job.get("active"))),
                "visible": int(bool(job.get("is_visible"))),
                "posted_at": job.get("date_posted"), "updated_at": job.get("date_updated"),
                "date_is_relative": int(bool(job.get("date_is_relative"))), "now": now})
            stored.append(posting_id)
        _flag_relevance(conn, "id IN (SELECT value FROM json_each(?))", [json.dumps(stored)])
        _regroup(conn, stored)
        _refresh_fts(conn, stored)
    if unsafe:
        ui.warn(f"[store] {source}: skipped {unsafe} posting(s) whose url is not http(s)")
    return stored


def mark_inactive_missing(source, seen_ids):
    """Deactivate this source's postings that its latest fetch no longer lists."""
    conn = connect()
    with conn:
        cur = conn.execute(
            "UPDATE postings SET active = 0 WHERE source = ? AND active = 1 "
            "AND id NOT IN (SELECT value FROM json_each(?))",
            (source, json.dumps(list(seen_ids))))
    return cur.rowcount


def rename_sources(mapping):
    conn = connect()
    with conn:
        conn.executemany("UPDATE postings SET source = ? WHERE source = ?",
                         [(new, old) for old, new in mapping.items()])


def _refresh_fts(conn, ids):
    ids = json.dumps(list(ids))
    conn.execute("DELETE FROM postings_fts WHERE id IN (SELECT value FROM json_each(?))",
                 (ids,))
    conn.execute(
        "INSERT INTO postings_fts (id, company, title, keywords) "
        "SELECT postings.id, postings.company, postings.title, descriptions.keywords "
        "FROM postings LEFT JOIN descriptions ON descriptions.posting_id = postings.id "
        "WHERE postings.id IN (SELECT value FROM json_each(?))", (ids,))


_SENT_MARKS = ", ".join("?" for _ in SENT)

# yes or no: what the summary says, else what the feed's sponsorship field implies
_SPONSORSHIP = """coalesce(descriptions.sponsorship, CASE postings.sponsorship
    WHEN 'Offers Sponsorship' THEN 'yes'
    WHEN 'Does Not Offer Sponsorship' THEN 'no'
    WHEN 'U.S. Citizenship is Required' THEN 'no' END)"""

# prior joins each company to its latest sent application, so a row can carry
# "applied before" without a per-row lookup
_DETAIL = f"""
SELECT postings.*, scores.fit, scores.tier, scores.below_floor, scores.fit_reason,
       scores.tier_reason, scores.run_id, descriptions.keywords, applications.status,
       applications.note,
       applications.applied_at, applications.updated_at AS status_updated_at,
       prior.applied_before_at, seen.posting_id IS NOT NULL AS seen,
       logos.domain AS logo_domain, feedback.verdict AS feedback_verdict,
       feedback.reason AS feedback_reason, descriptions.salary_min,
       descriptions.salary_max, descriptions.salary_period, descriptions.salary_currency,
       {_SPONSORSHIP} AS offers_sponsorship
FROM postings
LEFT JOIN scores ON scores.posting_id = postings.id
LEFT JOIN descriptions ON descriptions.posting_id = postings.id
LEFT JOIN feedback ON feedback.posting_id = postings.id
LEFT JOIN applications ON applications.posting_id = postings.id
LEFT JOIN (SELECT lower(p.company) AS company, max(a.applied_at) AS applied_before_at
           FROM applications a JOIN postings p ON p.id = a.posting_id
           WHERE a.status IN ({_SENT_MARKS}) GROUP BY lower(p.company)) prior
       ON prior.company = lower(postings.company)
LEFT JOIN seen ON seen.posting_id = postings.id
LEFT JOIN companies ON companies.name_key = name_key(postings.company)
LEFT JOIN logos ON logos.domain = companies.domain AND logos.path IS NOT NULL
"""
_DETAIL_PARAMS = list(SENT)


def get_posting(posting_id):
    row = connect().execute(_DETAIL + "WHERE postings.id = ?",
                            (*_DETAIL_PARAMS, posting_id)).fetchone()
    if row is None:
        return None
    job = _posting(row)
    _add_also_on([job])
    return job


def _add_also_on(jobs):
    """Set also_on on each job: the source and url of every other active posting in
    its group, earliest seen first."""
    members = connect().execute(
        "SELECT group_key, id, source, url FROM postings WHERE active = 1 "
        "AND group_key IN (SELECT value FROM json_each(?)) ORDER BY first_seen_at, id",
        (json.dumps([job["group_key"] for job in jobs if job.get("group_key")]),))
    by_group = {}
    for row in members:
        by_group.setdefault(row["group_key"], []).append(row)
    for job in jobs:
        job["also_on"] = [{"source": row["source"], "url": web_url(row["url"])}
                          for row in by_group.get(job.get("group_key"), ())
                          if row["id"] != job["id"]]


def resolve(prefix):
    """Full id for an exact match or an unambiguous prefix, else None.

    The TTY listing prints a short id prefix, and typing that prefix has to work.
    """
    conn = connect()
    if conn.execute("SELECT 1 FROM postings WHERE id = ?", (prefix,)).fetchone():
        return prefix
    matches = conn.execute("SELECT id FROM postings WHERE substr(id, 1, ?) = ? LIMIT 2",
                           (len(prefix), prefix)).fetchall()
    return matches[0]["id"] if len(matches) == 1 else None


def links_to_check(cfg, limit, checked_before, ids=None):
    """[{id, url}] of up to limit active, relevant postings whose link was never
    checked or last checked before checked_before, newest first: the scored ones, or
    with ids those among ids."""
    where, params = relevant_query(cfg)
    if ids is None:
        chosen = "postings.id IN (SELECT posting_id FROM scores)"
    else:
        chosen = "postings.id IN (SELECT value FROM json_each(?))"
        params.append(json.dumps(list(ids)))
    rows = connect().execute(
        f"SELECT postings.id, postings.url FROM postings "
        f"WHERE {where} AND {chosen} AND postings.url IS NOT NULL "
        f"AND (postings.link_checked_at IS NULL OR postings.link_checked_at < ?) "
        f"ORDER BY coalesce(postings.posted_at, 0) DESC, postings.id LIMIT ?",
        params + [checked_before, limit])
    return [dict(row) for row in rows]


def save_link_checks(outcomes):
    """Record [(id, "open" | "closed" | "unknown")] as checked now; unknown keeps the
    stored status."""
    now = db.now()
    conn = connect()
    with conn:
        conn.executemany(
            "UPDATE postings SET link_status = coalesce(nullif(?, 'unknown'), link_status), "
            "link_checked_at = ? WHERE id = ?",
            [(status, now, posting_id) for posting_id, status in outcomes])
