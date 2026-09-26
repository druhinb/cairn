"""Company websites and icons, and the companies still missing either."""
import datetime
import json
import time
from pathlib import Path

from cairn.core import settings
from cairn.store import db
from cairn.store.db import connect
from cairn.store.relevance import relevant_query

# a company whose website was not found is tried again after this many days
COMPANY_RETRY_DAYS = 30
# and an icon that failed to download after this many
ICON_RETRY_DAYS = 14


_UNRESOLVED_POSTINGS = """
SELECT name_key(company) AS name_key, company, company_url, url, id,
       coalesce(posted_at, 0) AS posted
FROM postings
WHERE active = 1 AND name_key(company) != ''
  AND NOT EXISTS (SELECT 1 FROM companies
                  WHERE companies.name_key = name_key(postings.company)
                    AND (companies.domain IS NOT NULL OR companies.resolved_at >= ?))
"""

# The companies of active postings, all of them or those whose name_key is in a JSON
# list, with what ranks them for an icon: the best fit plus tier of their scored
# postings, whether the saved preferences keep one of their postings, how many they
# have, and their newest. {relevant} is relevant_query's clause.
_PRIORITY = """
SELECT name_key(postings.company) AS name_key,
       max((SELECT scores.fit + scores.tier FROM scores
            WHERE scores.posting_id = postings.id)) AS best,
       max({relevant}) AS relevant, count(*) AS postings,
       max(coalesce(postings.posted_at, 0)) AS newest
FROM postings
WHERE postings.active = 1 AND name_key(postings.company) != ''
  AND (? IS NULL OR name_key(postings.company) IN (SELECT value FROM json_each(?)))
GROUP BY 1
"""

_BY_PRIORITY = ("priority.best IS NULL, priority.best DESC, priority.relevant DESC, "
                "priority.postings DESC, priority.newest DESC, priority.name_key")

_UNRESOLVED = f"""
SELECT name_key, company, company_url, url FROM ({_UNRESOLVED_POSTINGS}) unresolved
JOIN priority USING (name_key)
ORDER BY {_BY_PRIORITY}, posted DESC, id
"""

# companies whose website's icon failed, with a method after theirs and no failed
# move in COMPANY_RETRY_DAYS. CROSS JOIN keeps priority the inner loop, where the
# planner took it as the outer one and took 9 s over 17k postings.
_ICONLESS = f"""
SELECT companies.name_key, postings.company, postings.company_url, postings.url,
       companies.domain, companies.method
FROM postings
JOIN companies ON companies.name_key = name_key(postings.company)
JOIN logos ON logos.domain = companies.domain AND logos.path IS NULL
CROSS JOIN priority ON priority.name_key = companies.name_key
WHERE postings.active = 1 AND companies.method != ?
  AND (companies.error IS NULL OR companies.resolved_at < ?)
ORDER BY {_BY_PRIORITY}, coalesce(postings.posted_at, 0) DESC, postings.id
"""

# {join} is LEFT JOIN for every company's website, JOIN for those priority holds
_DOMAINS = f"""
SELECT companies.domain FROM companies
{{join}} priority USING (name_key)
WHERE companies.domain IS NOT NULL
ORDER BY priority.name_key IS NULL, {_BY_PRIORITY}, companies.domain
"""


def _priority(keys):
    """(WITH clause, params) naming _PRIORITY `priority`, over the companies keyed in
    keys, or every company when keys is None."""
    relevant, params = relevant_query(settings.get())
    keys = None if keys is None else json.dumps(list(keys))
    return f"WITH priority AS ({_PRIORITY.format(relevant=relevant)})", [*params, keys, keys]


def _ranked(sql, params, keys):
    """Rows of sql, a query over the `priority` table, given its params."""
    ranking, ranking_params = _priority(keys)
    return connect().execute(f"{ranking} {sql}", [*ranking_params, *params])


def _retry_cutoff():
    cutoff = datetime.datetime.now() - datetime.timedelta(days=COMPANY_RETRY_DAYS)
    return cutoff.isoformat(timespec="seconds")


def _by_company(rows, limit, skip):
    """(name_key, company name, company_url, apply urls, first row) for the first
    `limit` companies in rows whose name_key is not in skip. The apply urls are the
    first 5 of each."""
    found = {}
    for row in rows:
        key = row["name_key"]
        if key in skip:
            continue
        if key not in found:
            if len(found) == limit:
                continue
            found[key] = [row["company"], None, [], row]
        company = found[key]
        company[1] = company[1] or row["company_url"]
        if row["url"] and row["url"] not in company[2] and len(company[2]) < 5:
            company[2].append(row["url"])
    return [(key, *company) for key, company in found.items()]


def companies_without_domain(limit, skip=(), keys=None):
    """(name_key, company name, company_url, apply urls) for up to `limit` companies of
    active postings with no website and no failed attempt in COMPANY_RETRY_DAYS,
    leaving out the name_keys in skip, and with keys, those not in it. Ranked as a
    user sees them: companies with a scored posting first, the best fit plus tier
    first, then those with a posting the saved preferences keep, then the most
    active postings, then the newest. The apply urls are its 5 newest."""
    rows = _ranked(_UNRESOLVED, [_retry_cutoff()], keys)
    return [company[:4] for company in _by_company(rows, limit, skip)]


def companies_without_icon(limit, final_method, skip=(), keys=None):
    """(name_key, company name, company_url, apply urls, domain, method) for up to
    `limit` companies of active postings whose website's icon failed, whose method
    is not final_method, and whose last move failed more than COMPANY_RETRY_DAYS
    ago or never, leaving out the name_keys in skip, and with keys, those not in it.
    Ranked as companies_without_domain ranks them."""
    rows = _ranked(_ICONLESS, [final_method, _retry_cutoff()], keys)
    return [(*company[:4], company[4]["domain"], company[4]["method"])
            for company in _by_company(rows, limit, skip)]


def count_companies_without_domain():
    """How many companies companies_without_domain would return with no limit."""
    return connect().execute(f"SELECT count(DISTINCT name_key) FROM ({_UNRESOLVED_POSTINGS})",
                             (_retry_cutoff(),)).fetchone()[0]


def save_company(key, domain, method, error=None):
    conn = connect()
    with conn:
        conn.execute("INSERT OR REPLACE INTO companies VALUES (?, ?, ?, ?, ?)",
                     (key, domain, method, db.now(), error))


def company_domain(key):
    row = connect().execute("SELECT domain FROM companies WHERE name_key = ?",
                            (key,)).fetchone()
    return row["domain"] if row else None


def company_domains(keys=None):
    """Every website found for a company, or with keys, for the companies keyed in
    it, ranked by their companies as companies_without_domain ranks them."""
    sql = _DOMAINS.format(join="LEFT JOIN" if keys is None else "JOIN")
    return list(dict.fromkeys(row["domain"] for row in _ranked(sql, [], keys)))


def icon_domains(keys):
    """{name_key: website} for the companies keyed in keys whose website's icon is
    stored."""
    return {row["name_key"]: row["domain"] for row in connect().execute(
        "SELECT companies.name_key, companies.domain FROM companies "
        "JOIN logos ON logos.domain = companies.domain AND logos.path IS NOT NULL "
        "WHERE companies.name_key IN (SELECT value FROM json_each(?))",
        (json.dumps(list(keys)),))}


def logo(domain):
    row = connect().execute("SELECT domain, path, fetched_at, error, source FROM logos "
                            "WHERE domain = ?", (domain,)).fetchone()
    return dict(row) if row else None


def logos():
    return {row["domain"]: dict(row) for row in connect().execute(
        "SELECT domain, path, fetched_at, error, source FROM logos")}


def save_logo(domain, path, error=None, source=None):
    conn = connect()
    with conn:
        conn.execute("INSERT OR REPLACE INTO logos (domain, path, fetched_at, error, source) "
                     "VALUES (?, ?, ?, ?, ?)",
                     (domain, None if path is None else Path(path).name, time.time(), error,
                      source))


_ICON_COUNTS = """
SELECT count(*) AS companies, count(companies.domain) AS resolved,
       count(logos.path) AS with_icon,
       count(*) FILTER (WHERE companies.name_key IS NULL
                           OR (companies.domain IS NULL AND companies.resolved_at < :cutoff))
           AS pending,
       count(*) FILTER (WHERE (companies.domain IS NULL AND companies.resolved_at >= :cutoff)
                           OR (logos.domain IS NOT NULL AND logos.path IS NULL
                               AND logos.fetched_at >= :icon_cutoff))
           AS failed
FROM (SELECT DISTINCT name_key(company) AS name_key FROM postings
      WHERE active = 1 AND name_key(company) != '') active
LEFT JOIN companies USING (name_key)
LEFT JOIN logos ON logos.domain = companies.domain
"""


def icon_counts():
    """{companies, resolved, with_icon, pending, failed} over the companies of active
    postings: all of them, those whose website is known, those with a stored icon,
    those a fetch will look up (never tried, or failed longer ago than
    COMPANY_RETRY_DAYS), and those whose lookup or icon failed within its retry
    window."""
    params = {"cutoff": _retry_cutoff(),
              "icon_cutoff": time.time() - ICON_RETRY_DAYS * 86400}
    return dict(connect().execute(_ICON_COUNTS, params).fetchone())


def clear_icon_failures():
    """Forget every failed website lookup and icon fetch, and every failed move to a
    later website, so the next pass retries them. Returns (companies, icons)
    forgotten."""
    conn = connect()
    with conn:
        companies = conn.execute("DELETE FROM companies WHERE domain IS NULL").rowcount
        conn.execute("UPDATE companies SET error = NULL WHERE error IS NOT NULL")
        icons = conn.execute("DELETE FROM logos WHERE path IS NULL").rowcount
    return companies, icons
