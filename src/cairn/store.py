"""All pipeline state in one SQLite database at paths.db_file().

Every fetched posting is stored, relevant or not, so the relevance filter can change
without a refetch. `relevant_query` is the SQL form of fetch._relevant; a test holds
the two to the same answer on every branch of the rule.
"""
import _sqlite3
import ctypes
import datetime
import json
import os
import re
import sqlite3
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, parse_qsl, urlsplit

from cairn import paths, settings, ui

SCHEMA_VERSION = 12

# pipeline order; a posting with no applications row has no status, and `passed`
# keeps it out of the default jobs list
STATUSES = ("saved", "applied", "interviewing", "offer", "rejected", "withdrawn", "passed")
# the statuses that mean an application went out
SENT = ("applied", "interviewing", "offer", "rejected", "withdrawn")
# interview steps an application_events row can record
STAGES = ("screen", "oa", "onsite", "final", "other")
# the checklist an application gets on its first move to applied
DEFAULT_CHECKLIST = ("Resume version sent", "Referral asked", "Cover letter", "Thank-you note")

_SCORES = """
CREATE TABLE IF NOT EXISTS scores (
    posting_id TEXT PRIMARY KEY REFERENCES postings,
    fit INTEGER,
    tier INTEGER,
    below_floor INTEGER,
    fit_reason TEXT,
    tier_reason TEXT,
    scored_at TEXT,
    run_id INTEGER REFERENCES runs
)"""

_APPLICATIONS = """
CREATE TABLE IF NOT EXISTS applications (
    posting_id TEXT PRIMARY KEY REFERENCES postings,
    status TEXT NOT NULL,
    -- set by the first move into a SENT status and kept through every later one
    applied_at TEXT,
    note TEXT,
    created_at TEXT,
    updated_at TEXT
)"""

# a to-do list per application, shown in position order
_CHECKLIST = """
CREATE TABLE IF NOT EXISTS checklist (
    id INTEGER PRIMARY KEY,
    posting_id TEXT REFERENCES postings,
    label TEXT,
    done INTEGER,
    position INTEGER,
    created_at TEXT
)"""

# files the user keeps for an application, by absolute path; the file stays where it is
_ATTACHMENTS = """
CREATE TABLE IF NOT EXISTS attachments (
    id INTEGER PRIMARY KEY,
    posting_id TEXT REFERENCES postings,
    label TEXT,
    path TEXT,
    added_at TEXT
)"""

# one row per company icon fetch; path is the file's name in paths.home()/logos, NULL
# with error set when it failed. source is site, duckduckgo, or gstatic, whichever
# served the icon.
_LOGOS = """
CREATE TABLE IF NOT EXISTS logos (
    domain TEXT PRIMARY KEY,
    path TEXT,
    fetched_at REAL,
    error TEXT,
    source TEXT
)"""

# one row per company name as name_key() folds it; domain is NULL and error set
# when no website was found. A company whose website has no icon and whose later
# methods gave none keeps its domain, with error set.
_COMPANIES = """
CREATE TABLE IF NOT EXISTS companies (
    name_key TEXT PRIMARY KEY,
    domain TEXT,
    method TEXT,
    resolved_at TEXT,
    error TEXT
)"""

# a verdict on a posting's score; ranking prompts show the newest as calibration
_FEEDBACK = """
CREATE TABLE IF NOT EXISTS feedback (
    posting_id TEXT PRIMARY KEY REFERENCES postings,
    verdict TEXT CHECK (verdict IN ('up', 'down')),
    reason TEXT,
    note TEXT,
    created_at TEXT
)"""

FEEDBACK_REASONS = ("location", "compensation", "role", "company", "seniority", "other")

# one row per Claude CLI invocation, retries included; kind is rank, summary, or onboard
_CLAUDE_CALLS = """
CREATE TABLE IF NOT EXISTS claude_calls (
    id INTEGER PRIMARY KEY,
    made_at TEXT,
    kind TEXT,
    model TEXT,
    prompt_chars INTEGER,
    output_chars INTEGER,
    run_id INTEGER
)"""

# a company whose website was not found is tried again after this many days
COMPANY_RETRY_DAYS = 30
# and an icon that failed to download after this many
ICON_RETRY_DAYS = 14

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS postings (
    id TEXT PRIMARY KEY,
    source TEXT,
    company TEXT,
    title TEXT,
    url TEXT,
    url_key TEXT,
    locations TEXT,
    category TEXT,
    terms TEXT,
    degrees TEXT,
    sponsorship TEXT,
    company_url TEXT,
    -- the feed's own active flag, cleared once the posting's source stops listing it
    active INTEGER,
    visible INTEGER,
    posted_at REAL,
    updated_at REAL,
    first_seen_at TEXT,
    last_seen_at TEXT,
    -- postings of one company and title across sources share it; see group_key()
    group_key TEXT,
    -- open or closed from the last apply-link check, NULL before one decided
    link_status TEXT,
    link_checked_at TEXT,
    -- 1 when the posting passes the relevance rule, active and visible aside, under
    -- the settings meta's relevance_rules records; see relevant_query()
    relevant INTEGER
);
CREATE INDEX IF NOT EXISTS postings_url ON postings(url);
CREATE INDEX IF NOT EXISTS postings_url_key ON postings(url_key);
CREATE INDEX IF NOT EXISTS postings_company ON postings(company);
CREATE INDEX IF NOT EXISTS postings_posted_at ON postings(posted_at);

{_SCORES};

CREATE TABLE IF NOT EXISTS descriptions (
    posting_id TEXT PRIMARY KEY REFERENCES postings,
    text TEXT,
    source TEXT,
    error TEXT,
    keywords TEXT,
    fetched_at TEXT,
    -- parsed from the SALARY and SPONSORSHIP summary lines; period is year, month, or
    -- hour, currency an ISO code, and sponsorship yes or no
    salary_min INTEGER,
    salary_max INTEGER,
    salary_period TEXT,
    salary_currency TEXT,
    sponsorship TEXT
);

{_APPLICATIONS};

{_LOGOS};

{_COMPANIES};

{_FEEDBACK};

{_CLAUDE_CALLS};
CREATE INDEX IF NOT EXISTS claude_calls_made_at ON claude_calls(made_at);

{_CHECKLIST};
CREATE INDEX IF NOT EXISTS checklist_posting ON checklist(posting_id);

{_ATTACHMENTS};

-- one row per change of status, with stage NULL, or per interview stage, with status
-- NULL and at the time the stage happens, which may be in the future. A stage keeps
-- when it was written and last changed, which name its calendar event and revision.
CREATE TABLE IF NOT EXISTS application_events (
    id INTEGER PRIMARY KEY,
    posting_id TEXT REFERENCES postings,
    status TEXT,
    at TEXT,
    note TEXT,
    stage TEXT,
    created_at TEXT,
    updated_at TEXT
);
CREATE INDEX IF NOT EXISTS application_events_posting ON application_events(posting_id);

-- legacy seen.json ids may have no postings row, so no foreign key
CREATE TABLE IF NOT EXISTS seen (
    posting_id TEXT PRIMARY KEY,
    seen_at TEXT
);

CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY,
    started_at TEXT,
    finished_at TEXT,
    status TEXT,
    counts TEXT,
    log_path TEXT,
    -- byte range [log_start, log_end) of log_path from run_start through the closing
    -- run_done or run_failed line, written while this run held the run lock; an
    -- in-process emitter outside the run (a server thread) may interleave
    log_start INTEGER,
    log_end INTEGER
);

CREATE VIRTUAL TABLE IF NOT EXISTS postings_fts
    USING fts5(id UNINDEXED, company, title, keywords);
"""

# connect() runs SCHEMA before migrating, when older tables lack these columns
_AFTER_MIGRATION = """
CREATE INDEX IF NOT EXISTS postings_group ON postings(group_key);
CREATE INDEX IF NOT EXISTS postings_relevant ON postings(relevant, active, visible);
CREATE INDEX IF NOT EXISTS scores_run ON scores(run_id);
"""

def _recompute_url_keys(conn):
    rows = conn.execute("SELECT id, url, company, title FROM postings").fetchall()
    conn.executemany("UPDATE postings SET url_key = ? WHERE id = ?", [
        (url_key({"url": r["url"], "company_name": r["company"], "title": r["title"]}),
         r["id"]) for r in rows])


def _track_statuses(conn):
    conn.execute("ALTER TABLE applications RENAME TO applications_v4")
    conn.execute(_APPLICATIONS)
    conn.execute("INSERT INTO applications (posting_id, status, applied_at, note, created_at, "
                 "updated_at) SELECT posting_id, 'applied', applied_at, note, applied_at, "
                 "applied_at FROM applications_v4")
    conn.execute("INSERT INTO application_events (posting_id, status, at) "
                 "SELECT posting_id, 'applied', applied_at FROM applications_v4 "
                 "ORDER BY applied_at")
    conn.execute("DROP TABLE applications_v4")


def _record_score_runs(conn):
    conn.execute("ALTER TABLE scores RENAME TO scores_v4")
    conn.execute(_SCORES)
    # a scores table that this connect()'s SCHEMA created has the current columns
    old = {row["name"] for row in conn.execute("PRAGMA table_info(scores_v4)")}
    reason = "reason" if "reason" in old else "fit_reason"
    conn.execute("INSERT INTO scores (posting_id, fit, tier, below_floor, fit_reason, "
                 f"scored_at) SELECT posting_id, fit, tier, below_floor, {reason}, scored_at "
                 "FROM scores_v4")
    conn.execute("DROP TABLE scores_v4")


def _add_company_url(conn):
    # connect() runs SCHEMA before migrating, so a postings table missing until now
    # was just created with the column
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(postings)")}
    if "company_url" not in columns:
        conn.execute("ALTER TABLE postings ADD COLUMN company_url TEXT")


def _add_logo_source(conn):
    # a logos table created by this connect() already has the column
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(logos)")}
    if "source" not in columns:
        conn.execute("ALTER TABLE logos ADD COLUMN source TEXT")
    # every icon stored before version 8 came from the company's own site, under an
    # absolute path that a moved home no longer matched
    rows = conn.execute("SELECT domain, path FROM logos WHERE path IS NOT NULL").fetchall()
    conn.executemany("UPDATE logos SET path = ?, source = 'site' WHERE domain = ?",
                     [(Path(row["path"]).name, row["domain"]) for row in rows])


_V9_COLUMNS = {
    "postings": ("group_key TEXT", "link_status TEXT", "link_checked_at TEXT"),
    "descriptions": ("salary_min INTEGER", "salary_max INTEGER", "salary_period TEXT",
                     "salary_currency TEXT", "sponsorship TEXT"),
}


def _add_v9_columns(conn):
    for table, columns in _V9_COLUMNS.items():
        present = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        for column in columns:
            if column.split()[0] not in present:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column}")


def _group_every_posting(conn):
    _assign_groups(conn, conn.execute(_GROUP_COLUMNS).fetchall())


def _add_event_columns(conn):
    present = {row["name"] for row in conn.execute("PRAGMA table_info(application_events)")}
    for column in ("stage", "created_at", "updated_at"):
        if column not in present:
            conn.execute(f"ALTER TABLE application_events ADD COLUMN {column} TEXT")


def _split_score_reason(conn):
    # a scores table rebuilt by migration 5 in this connect() already has both columns
    present = {row["name"] for row in conn.execute("PRAGMA table_info(scores)")}
    if "reason" in present:
        conn.execute("ALTER TABLE scores RENAME COLUMN reason TO fit_reason")
    if "tier_reason" not in present:
        conn.execute("ALTER TABLE scores ADD COLUMN tier_reason TEXT")


def _add_relevant(conn):
    # a postings table created by this connect() already has the column
    present = {row["name"] for row in conn.execute("PRAGMA table_info(postings)")}
    if "relevant" not in present:
        conn.execute("ALTER TABLE postings ADD COLUMN relevant INTEGER")


# steps that bring a database at version N - 1 up to version N: SQL, or a function
# of the connection for a change SQL cannot express
_MIGRATIONS = {
    2: ("ALTER TABLE runs ADD COLUMN log_start INTEGER",
        "ALTER TABLE runs ADD COLUMN log_end INTEGER"),
    # url_key started keeping gh_jid, and upserts match stored rows by url_key
    3: (_recompute_url_keys,),
    # resume tailoring was removed along with the tables it kept
    4: ("DROP TABLE IF EXISTS resumes",
        "DROP TABLE IF EXISTS tailored"),
    # applications became a status pipeline with a history, and a score records the
    # run that wrote it; both tables are rebuilt so they match a fresh database
    5: (_track_statuses, _record_score_runs),
    6: (_add_company_url, _LOGOS),
    7: (_COMPANIES,),
    8: (_add_logo_source,),
    # score feedback, the Claude call log, cross-source groups, link checks, and the
    # salary and sponsorship a summary states
    9: (_add_v9_columns, _FEEDBACK, _CLAUDE_CALLS, _group_every_posting),
    # interview stages in the history, and a checklist and files per application; a
    # version 9 database written before salary_currency joined _V9_COLUMNS lacks it
    10: (_add_event_columns, _CHECKLIST, "CREATE INDEX IF NOT EXISTS checklist_posting "
         "ON checklist(posting_id)", _ATTACHMENTS, _add_v9_columns),
    # a score gives a reason per axis; the one reason that covered both reads
    # mostly as the role's fit, so it stays as the fit reason
    11: (_split_score_reason,),
    # relevance is stored per posting; connect() computes it once the column exists
    12: (_add_relevant,),
}

# One connection per (path, thread). A shared connection would let a background
# run's `with conn:` transaction absorb a server handler's writes. A server
# request thread keeps its connection for as long as the pool keeps the thread,
# which anyio's thread limiter bounds; background job threads call close_thread().
_connections = {}
_connections_lock = threading.Lock()

_SQLITE_CONFIG_MEMSTATUS = 9


def _stop_memory_stats():
    """Turn off SQLite's allocation statistics, which put every allocation of every
    thread behind one mutex: four request threads each took 1,030 ms over a query
    that took 80 ms alone.

    SQLite takes the setting only while shut down, and the sqlite3 module starts it
    on import. Shutting it down with a connection open is undefined, so this runs
    once, at import, before this module has opened any.
    """
    try:
        # uv's Pythons build _sqlite3 into the interpreter, which CDLL(None) opens
        lib = ctypes.CDLL(getattr(_sqlite3, "__file__", None))
        config = lib.sqlite3_config
        # the option is the one fixed parameter; on arm64 the value after it is
        # read from the stack
        config.argtypes = [ctypes.c_int]
        lib.sqlite3_shutdown()
        configured = config(_SQLITE_CONFIG_MEMSTATUS, ctypes.c_int(0))
        started = lib.sqlite3_initialize()
    except (OSError, AttributeError) as e:
        ui.warn(f"[store] SQLite allocation statistics stay on, so parallel queries "
                f"queue: {type(e).__name__}: {e}")
        return
    if configured or started:
        ui.warn(f"[store] SQLite allocation statistics stay on, so parallel queries "
                f"queue: sqlite3_config returned {configured}, sqlite3_initialize "
                f"{started}")


_stop_memory_stats()


def _now():
    return datetime.datetime.now().isoformat(timespec="seconds")


def name_key(name):
    """A company name casefolded with its whitespace collapsed, the companies key."""
    return None if name is None else " ".join(name.casefold().split())


def connect():
    """This thread's connection for the current home, opened, migrated, and imported
    on first use."""
    path = paths.db_file().resolve()
    key = (path, threading.get_ident())
    with _connections_lock:
        conn = _connections.get(key)
    if conn is not None:
        return conn
    path.parent.mkdir(parents=True, exist_ok=True)
    # each connection is used only by its own thread; the flag lets close() run
    # from any thread
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    # a restored database is untrusted input: its schema may not call functions with
    # side effects, or rewrite sqlite_schema
    conn.execute("PRAGMA trusted_schema = OFF")
    if hasattr(conn, "setconfig"):  # Python 3.12+
        conn.setconfig(sqlite3.SQLITE_DBCONFIG_DEFENSIVE, True)
    conn.create_function("name_key", 1, name_key, deterministic=True)
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        conn.executescript(SCHEMA)
        with conn:
            conn.execute("INSERT OR IGNORE INTO meta VALUES ('schema_version', ?)",
                         (str(SCHEMA_VERSION),))
        _migrate(conn)
        conn.executescript(_AFTER_MIGRATION)
        _import_legacy(conn, paths.state_dir())
        try:
            _sync_relevance(conn, settings.base())
        except settings.SettingsError:
            # relevant_query tests the rule itself until the settings load, and every
            # settings.get() reports the error
            pass
    except BaseException:
        conn.close()
        raise
    with _connections_lock:
        _connections[key] = conn
    return conn


def _schema_version(conn):
    return int(conn.execute(
        "SELECT value FROM meta WHERE key = 'schema_version'").fetchone()[0])


def _migrate(conn):
    if _schema_version(conn) >= SCHEMA_VERSION:
        return
    with conn:
        # IMMEDIATE takes the write lock up front, and sqlite3 would otherwise run
        # the DDL outside any transaction
        conn.execute("BEGIN IMMEDIATE")
        # a process that migrated while this one waited has already done the work
        version = _schema_version(conn)
        for target in range(version + 1, SCHEMA_VERSION + 1):
            for step in _MIGRATIONS[target]:
                if callable(step):
                    step(conn)
                else:
                    conn.execute(step)
        if version < SCHEMA_VERSION:
            conn.execute("UPDATE meta SET value = ? WHERE key = 'schema_version'",
                         (str(SCHEMA_VERSION),))


def close():
    """Close every thread's connection to the current home's database."""
    path = paths.db_file().resolve()
    with _connections_lock:
        doomed = [_connections.pop(k) for k in list(_connections) if k[0] == path]
    for conn in doomed:
        conn.close()


def close_thread():
    """Close the calling thread's connections, whichever home they belong to."""
    ident = threading.get_ident()
    with _connections_lock:
        doomed = [_connections.pop(k) for k in list(_connections) if k[1] == ident]
    for conn in doomed:
        conn.close()


# --------------------------------------------------------------------------
# Postings
# --------------------------------------------------------------------------
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


def _json_list(value):
    return None if value is None else json.dumps(list(value), ensure_ascii=False)


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
    now = _now()
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
                "locations": _json_list(job.get("locations")),
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
    """Relabel postings stored under each old source name with its new one."""
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


def _list(text):
    return json.loads(text) if text else []


_POSTING_COLUMNS = ("id", "source", "company", "title", "url", "url_key", "locations",
                    "category", "terms", "degrees", "sponsorship", "company_url",
                    "active", "visible", "posted_at", "updated_at", "first_seen_at",
                    "last_seen_at", "link_status", "relevant")


def _fold_detail(job):
    """Replace the flat feedback and salary columns of a _DETAIL row with one value each."""
    verdict, reason = job.pop("feedback_verdict"), job.pop("feedback_reason")
    job["feedback"] = {"verdict": verdict, "reason": reason} if verdict else None
    salary = {"min": job.pop("salary_min"), "max": job.pop("salary_max"),
              "period": job.pop("salary_period"), "currency": job.pop("salary_currency")}
    job["salary"] = salary if salary["min"] is not None or salary["max"] is not None else None


def _posting(row):
    """A stored posting in the raw feed shape, plus whatever the joins brought."""
    job = {"id": row["id"], "source": row["source"], "company_name": row["company"],
           "title": row["title"], "url": row["url"], "locations": _list(row["locations"]),
           "category": row["category"], "terms": _list(row["terms"]),
           "degrees": _list(row["degrees"]), "sponsorship": row["sponsorship"],
           "company_url": row["company_url"], "active": bool(row["active"]),
           "is_visible": bool(row["visible"]),
           "date_posted": row["posted_at"], "date_updated": row["updated_at"],
           "first_seen_at": row["first_seen_at"], "last_seen_at": row["last_seen_at"],
           "closed": row["link_status"] == "closed"}
    for name in sorted(set(row.keys()) - set(_POSTING_COLUMNS)):
        job[name] = row[name]
    if "feedback_verdict" in job:
        _fold_detail(job)
    for name in ("below_floor", "seen"):
        if job.get(name) is not None:
            job[name] = bool(job[name])
    return job


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

    The TTY listing shows a short id prefix to keep the table readable, so typing
    that prefix has to work. An ambiguous prefix resolves to nothing rather than
    guessing which posting you meant.
    """
    conn = connect()
    if conn.execute("SELECT 1 FROM postings WHERE id = ?", (prefix,)).fetchone():
        return prefix
    matches = conn.execute("SELECT id FROM postings WHERE substr(id, 1, ?) = ? LIMIT 2",
                           (len(prefix), prefix)).fetchall()
    return matches[0]["id"] if len(matches) == 1 else None


# --------------------------------------------------------------------------
# Relevance, as SQL over the postings table
# --------------------------------------------------------------------------
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
        add(*_any_substring(_LOCATIONS, [l.lower() for l in cfg.location_allow]))

    return "(" + " AND ".join(clauses) + ")", params


# the settings _relevance_rule reads
_RELEVANCE_SETTINGS = ("title_exclude", "title_exclude_field", "intern_terms",
                       "wanted_intern_terms", "include_off_season_internships",
                       "allowed_categories", "title_keywords", "degrees_held",
                       "location_allow")


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


# --------------------------------------------------------------------------
# Search
# --------------------------------------------------------------------------
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

    NUL is dropped because FTS5 rejects it even inside a quoted string.
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
             posted_within_days=None, active_only=True, run_id=None, sponsorship=None,
             salary_min=None):
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
    given = {"fit_min": fit_min, "tier_min": tier_min, "location": location,
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
    return " AND ".join(clauses) or "1", params


def search(q=None, relevant_only=True, fit_min=None, tier_min=None, status=None,
           hide_passed=True, category=None, location=None, source=None,
           posted_within_days=None, active_only=True, run_id=None, sponsorship=None,
           salary_min=None, sort="score", limit=50, offset=0):
    """(rows, total): one page of matching postings and the count across all pages.

    status lists STATUSES entries, and "none" for postings that have no status.
    hide_passed drops `passed` postings unless status names that status. category
    and source take one value or a list; run_id keeps the postings scored in that run.
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
        source=source, posted_within_days=posted_within_days, active_only=active_only,
        run_id=run_id, sponsorship=sponsorship, salary_min=salary_min)
    chosen = _representatives(where)
    conn = connect()
    total = conn.execute(f"SELECT count(*) FROM ({chosen})", params).fetchone()[0]
    rows = conn.execute(f"{_DETAIL} WHERE postings.id IN ({chosen}) "
                        f"ORDER BY {_SORTS[sort]}, postings.id LIMIT ? OFFSET ?",
                        _DETAIL_PARAMS + params + [limit, offset]).fetchall()
    jobs = [_posting(row) for row in rows]
    _add_also_on(jobs)
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
    filter left out, so a value's count is the total that choosing it would give.

    The postings every facet shares are found once; each facet then counts them
    under the other facets' filters.
    """
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


# --------------------------------------------------------------------------
# Seen
# --------------------------------------------------------------------------
def mark_seen(ids):
    """Record ids as seen. Returns the new total."""
    conn = connect()
    now = _now()
    with conn:
        conn.executemany("INSERT OR IGNORE INTO seen VALUES (?, ?)",
                         [(i, now) for i in ids if i])
    return conn.execute("SELECT count(*) FROM seen").fetchone()[0]


def seen_ids():
    return {row[0] for row in connect().execute("SELECT posting_id FROM seen")}


def is_seen(posting_id):
    return connect().execute("SELECT 1 FROM seen WHERE posting_id = ?",
                             (posting_id,)).fetchone() is not None


# --------------------------------------------------------------------------
# Scores and descriptions
# --------------------------------------------------------------------------
def save_scores(results, run_id=None):
    now = _now()
    rows = [(r["id"], r["fit"], r.get("tier"), int(bool(r.get("below_floor"))),
             r.get("fit_reason"), r.get("tier_reason"), now, run_id)
            for r in results if r.get("id") and r.get("fit") is not None]
    conn = connect()
    with conn:
        conn.executemany("INSERT OR REPLACE INTO scores (posting_id, fit, tier, below_floor, "
                         "fit_reason, tier_reason, scored_at, run_id) "
                         "VALUES (?, ?, ?, ?, ?, ?, ?, ?)", rows)
    return len(rows)


def save_reasons(posting_id, fit_reason, tier_reason):
    """Set the reasons of a stored score, leaving the score and its run as they are."""
    conn = connect()
    with conn:
        conn.execute("UPDATE scores SET fit_reason = ?, tier_reason = ? WHERE posting_id = ?",
                     (fit_reason, tier_reason, posting_id))


def get_description(posting_id):
    row = connect().execute(
        "SELECT text, source, error, keywords, fetched_at FROM descriptions "
        "WHERE posting_id = ?", (posting_id,)).fetchone()
    return dict(row) if row else None


def save_description(posting_id, text, source, error, keywords=None):
    conn = connect()
    with conn:
        conn.execute("INSERT OR REPLACE INTO descriptions (posting_id, text, source, error, "
                     "keywords, fetched_at) VALUES (?, ?, ?, ?, ?, ?)",
                     (posting_id, text, source, error, keywords, _now()))
        _refresh_fts(conn, [posting_id])


_SET_KEYWORDS = """
INSERT INTO descriptions (posting_id, keywords, fetched_at, salary_min, salary_max,
                          salary_period, salary_currency, sponsorship)
VALUES (:id, :keywords, :now, :min, :max, :period, :currency, :sponsorship)
ON CONFLICT(posting_id) DO UPDATE SET
    keywords = excluded.keywords, salary_min = excluded.salary_min,
    salary_max = excluded.salary_max, salary_period = excluded.salary_period,
    salary_currency = excluded.salary_currency, sponsorship = excluded.sponsorship
"""


def set_keywords(posting_id, keywords, salary=None, sponsorship=None):
    """Store a posting's summary with the salary {min, max, period, currency} and the
    sponsorship (True, False, or None) it states."""
    salary = salary or {}
    conn = connect()
    with conn:
        conn.execute(_SET_KEYWORDS, {
            "id": posting_id, "keywords": keywords, "now": _now(), "min": salary.get("min"),
            "max": salary.get("max"), "period": salary.get("period"),
            "currency": salary.get("currency"),
            "sponsorship": None if sponsorship is None else ("yes" if sponsorship else "no")})
        _refresh_fts(conn, [posting_id])


# --------------------------------------------------------------------------
# Score feedback
# --------------------------------------------------------------------------
def set_feedback(posting_id, verdict, reason=None, note=None):
    """Record a verdict of up or down on a posting's score, replacing any earlier one.

    Raises ValueError for a verdict or reason outside the allowed ones.
    """
    if verdict not in ("up", "down"):
        raise ValueError(f"unknown verdict {verdict!r}; expected up or down")
    if reason is not None and reason not in FEEDBACK_REASONS:
        raise ValueError(f"unknown reason {reason!r}; expected one of "
                         f"{', '.join(FEEDBACK_REASONS)}")
    conn = connect()
    with conn:
        conn.execute("INSERT OR REPLACE INTO feedback VALUES (?, ?, ?, ?, ?)",
                     (posting_id, verdict, reason, note, _now()))
    return feedback(posting_id)


def clear_feedback(posting_id):
    conn = connect()
    with conn:
        conn.execute("DELETE FROM feedback WHERE posting_id = ?", (posting_id,))


def feedback(posting_id):
    row = connect().execute("SELECT verdict, reason, note, created_at FROM feedback "
                            "WHERE posting_id = ?", (posting_id,)).fetchone()
    return dict(row) if row else None


_FEEDBACK_EXAMPLES = """
SELECT postings.company, postings.title, scores.fit, scores.tier, feedback.verdict,
       feedback.reason, feedback.note, feedback.created_at
FROM feedback
JOIN postings ON postings.id = feedback.posting_id
JOIN scores ON scores.posting_id = feedback.posting_id
WHERE feedback.verdict = ?
ORDER BY feedback.created_at DESC, feedback.posting_id
LIMIT ?
"""


def feedback_examples(limit=12):
    """Up to limit scored postings with feedback, newest first, half up and half down
    when both have enough, the rest from whichever has more."""
    conn = connect()
    ups, downs = ([dict(row) for row in conn.execute(_FEEDBACK_EXAMPLES, (verdict, limit))]
                  for verdict in ("up", "down"))
    take_up = min(len(ups), max(limit // 2, limit - len(downs)))
    chosen = ups[:take_up] + downs[:limit - take_up]
    return sorted(chosen, key=lambda row: row["created_at"], reverse=True)


def feedback_stats():
    row = connect().execute(
        "SELECT count(*) FILTER (WHERE verdict = 'up') AS up, "
        "count(*) FILTER (WHERE verdict = 'down') AS down, count(*) AS total "
        "FROM feedback").fetchone()
    return dict(row)


# --------------------------------------------------------------------------
# Claude calls
# --------------------------------------------------------------------------
CALL_KINDS = ("rank", "summary", "onboard")


def record_call(kind, model, prompt_chars, output_chars, run_id=None):
    conn = connect()
    with conn:
        conn.execute("INSERT INTO claude_calls (made_at, kind, model, prompt_chars, "
                     "output_chars, run_id) VALUES (?, ?, ?, ?, ?, ?)",
                     (_now(), kind, model, prompt_chars, output_chars, run_id))


def call_counts(days=30):
    """{rank, summary, onboard, total, prompt_chars} over the last `days` days."""
    since = (datetime.datetime.now() - datetime.timedelta(days=days)).isoformat(
        timespec="seconds")
    rows = connect().execute("SELECT kind, count(*), coalesce(sum(prompt_chars), 0) "
                             "FROM claude_calls WHERE made_at >= ? GROUP BY kind", (since,))
    found = {kind: (n, chars) for kind, n, chars in rows}
    counted = {kind: found.get(kind, (0, 0))[0] for kind in CALL_KINDS}
    return {**counted, "total": sum(n for n, _ in found.values()),
            "prompt_chars": sum(chars for _, chars in found.values())}


# --------------------------------------------------------------------------
# Apply-link checks
# --------------------------------------------------------------------------
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
    now = _now()
    conn = connect()
    with conn:
        conn.executemany(
            "UPDATE postings SET link_status = coalesce(nullif(?, 'unknown'), link_status), "
            "link_checked_at = ? WHERE id = ?",
            [(status, now, posting_id) for posting_id, status in outcomes])


# --------------------------------------------------------------------------
# Company icons
# --------------------------------------------------------------------------
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
                     (key, domain, method, _now(), error))


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
    """Every recorded icon fetch, keyed by domain."""
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


# --------------------------------------------------------------------------
# Applications
# --------------------------------------------------------------------------
_SET_STATUS = """
INSERT INTO applications (posting_id, status, applied_at, note, created_at, updated_at)
VALUES (:id, :status, :applied_at, :note, :now, :now)
ON CONFLICT(posting_id) DO UPDATE SET
    status = excluded.status,
    applied_at = coalesce(applications.applied_at, excluded.applied_at),
    note = coalesce(excluded.note, applications.note),
    updated_at = excluded.updated_at
"""

# _now()'s format, which every stored time uses
_SQL_NOW = "strftime('%Y-%m-%dT%H:%M:%S', 'now', 'localtime')"

_APPLICATION_ROWS = f"""
SELECT applications.posting_id AS id, applications.status, applications.applied_at,
       applications.note, applications.created_at, applications.updated_at,
       postings.company, postings.title, postings.url, postings.locations,
       postings.source, postings.posted_at, scores.fit, scores.tier, scores.below_floor,
       logos.domain AS logo_domain,
       (SELECT json_object('stage', stage, 'at', at) FROM application_events
        WHERE posting_id = applications.posting_id AND stage IS NOT NULL
          AND at >= {_SQL_NOW}
        ORDER BY at, id LIMIT 1) AS next_stage,
       (SELECT json_object('done', count(*) FILTER (WHERE done), 'total', count(*))
        FROM checklist WHERE posting_id = applications.posting_id) AS checklist
FROM applications
JOIN postings ON postings.id = applications.posting_id
LEFT JOIN scores ON scores.posting_id = applications.posting_id
LEFT JOIN companies ON companies.name_key = name_key(postings.company)
LEFT JOIN logos ON logos.domain = companies.domain AND logos.path IS NOT NULL
"""


def _application(row):
    below_floor = row["below_floor"]
    next_stage = row["next_stage"]
    return {**dict(row), "locations": _list(row["locations"]),
            "below_floor": None if below_floor is None else bool(below_floor),
            "next_stage": json.loads(next_stage) if next_stage else None,
            "checklist": json.loads(row["checklist"])}


def _change_status(conn, posting_id, status, note, now):
    """Write status inside the caller's IMMEDIATE transaction, logging a change and
    giving a move to applied the default checklist when the posting has none."""
    before = conn.execute("SELECT status FROM applications WHERE posting_id = ?",
                          (posting_id,)).fetchone()
    conn.execute(_SET_STATUS, {"id": posting_id, "status": status, "note": note,
                               "applied_at": now if status in SENT else None, "now": now})
    if before is not None and before["status"] == status:
        return
    conn.execute("INSERT INTO application_events (posting_id, status, at, note) "
                 "VALUES (?, ?, ?, ?)", (posting_id, status, now, note))
    if status == "applied" and not conn.execute(
            "SELECT 1 FROM checklist WHERE posting_id = ?", (posting_id,)).fetchone():
        conn.executemany("INSERT INTO checklist (posting_id, label, done, position, "
                         "created_at) VALUES (?, ?, 0, ?, ?)",
                         [(posting_id, label, position, now)
                          for position, label in enumerate(DEFAULT_CHECKLIST)])


def set_status(posting_id, status, note=None):
    """Move a posting to status and return its application row.

    Each change of status is appended to its history. The first move into a SENT
    status sets applied_at, which later moves keep. A move to applied gives a
    posting with an empty checklist DEFAULT_CHECKLIST. note replaces the
    stored note unless it is None. Raises ValueError for a status outside STATUSES.
    """
    if status not in STATUSES:
        raise ValueError(f"unknown status {status!r}; expected one of {', '.join(STATUSES)}")
    conn = connect()
    with conn:
        # the event depends on the status read here, so the read takes the write lock
        conn.execute("BEGIN IMMEDIATE")
        _change_status(conn, posting_id, status, note, _now())
    return application(posting_id)


def set_note(posting_id, note):
    """Replace the note of a posting that has a status. Returns its row, or None."""
    conn = connect()
    with conn:
        cur = conn.execute("UPDATE applications SET note = ?, updated_at = ? "
                           "WHERE posting_id = ?", (note, _now(), posting_id))
    return application(posting_id) if cur.rowcount else None


def clear_application(posting_id):
    """Forget a posting's status, note, history, stages, checklist, and files."""
    conn = connect()
    with conn:
        for table in ("application_events", "checklist", "attachments"):
            conn.execute(f"DELETE FROM {table} WHERE posting_id = ?", (posting_id,))
        conn.execute("DELETE FROM applications WHERE posting_id = ?", (posting_id,))


def application(posting_id):
    row = connect().execute(_APPLICATION_ROWS + "WHERE applications.posting_id = ?",
                            (posting_id,)).fetchone()
    return _application(row) if row else None


def application_events(posting_id):
    """A posting's changes of status and its stages, {id, status, stage, at, note}
    each, in time order. A status change has stage None and a stage status None."""
    rows = connect().execute("SELECT id, status, stage, at, note FROM application_events "
                             "WHERE posting_id = ? ORDER BY at, id", (posting_id,))
    return [dict(row) for row in rows]


def applications(statuses=None):
    """Every application row with its posting, most recently updated first.

    statuses, when given, keeps only the rows holding one of them.
    """
    where, params = "", []
    if statuses is not None:
        where, params = f"WHERE applications.status IN ({_marks(statuses)})", list(statuses)
    rows = connect().execute(f"{_APPLICATION_ROWS} {where} ORDER BY "
                             "applications.updated_at DESC, applications.posting_id", params)
    return [_application(row) for row in rows]


def application_counts():
    """status -> how many postings hold it, for every status."""
    found = {status: n for status, n in connect().execute(
        "SELECT status, count(*) FROM applications GROUP BY status")}
    return {status: found.get(status, 0) for status in STATUSES}


# --------------------------------------------------------------------------
# Interview stages
# --------------------------------------------------------------------------
def _stage_name(stage):
    if stage not in STAGES:
        raise ValueError(f"unknown stage {stage!r}; expected one of {', '.join(STAGES)}")
    return stage


def _stage_time(at):
    """at, an ISO date or datetime, as a local time in _now()'s format."""
    try:
        when = datetime.datetime.fromisoformat(at) if isinstance(at, str) else None
    except ValueError:
        when = None
    if when is None:
        raise ValueError(f"{at!r} is not an ISO date or time")
    if when.tzinfo is not None:
        try:
            when = when.astimezone().replace(tzinfo=None)
        except OverflowError:
            raise ValueError(f"{at!r} is out of range") from None
    return when.isoformat(timespec="seconds")


_STAGE = "SELECT id, posting_id, status, stage, at, note FROM application_events "


def get_stage(event_id):
    row = connect().execute(_STAGE + "WHERE id = ? AND stage IS NOT NULL",
                            (event_id,)).fetchone()
    return dict(row) if row else None


def add_stage(posting_id, stage, at, note=None):
    """Record an interview stage at `at`, past or future, and return its event.

    A posting with no application gets one in status interviewing. Raises
    ValueError for a stage outside STAGES or an `at` that is not ISO 8601.
    """
    stage, at = _stage_name(stage), _stage_time(at)
    conn, now = connect(), _now()
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        if not conn.execute("SELECT 1 FROM applications WHERE posting_id = ?",
                            (posting_id,)).fetchone():
            _change_status(conn, posting_id, "interviewing", None, now)
        cur = conn.execute("INSERT INTO application_events (posting_id, stage, at, note, "
                           "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                           (posting_id, stage, at, note, now, now))
    return get_stage(cur.lastrowid)


def update_stage(event_id, stage=None, at=None, note=None):
    """Change what is given of a stage and stamp its updated_at. Returns the event,
    or None for no such stage.

    Raises ValueError as add_stage does, and for a change that gives nothing.
    """
    changes = {}
    if stage is not None:
        changes["stage"] = _stage_name(stage)
    if at is not None:
        changes["at"] = _stage_time(at)
    if note is not None:
        changes["note"] = note
    if not changes:
        raise ValueError("nothing to change")
    changes["updated_at"] = _now()
    conn = connect()
    with conn:
        conn.execute(f"UPDATE application_events SET "
                     f"{', '.join(f'{name} = ?' for name in changes)} "
                     f"WHERE id = ? AND stage IS NOT NULL", [*changes.values(), event_id])
    return get_stage(event_id)


def delete_stage(event_id):
    """Delete a stage. Returns its posting id, or None for no such stage."""
    found = get_stage(event_id)
    if found:
        conn = connect()
        with conn:
            conn.execute("DELETE FROM application_events WHERE id = ?", (event_id,))
    return found and found["posting_id"]


# a rejected or withdrawn application's stages are over whatever their time
_STAGE_ROWS = """
SELECT application_events.id AS event_id, postings.id, postings.company, postings.title,
       postings.url, applications.status, application_events.stage, application_events.at,
       application_events.note, application_events.created_at, application_events.updated_at
FROM application_events
JOIN postings ON postings.id = application_events.posting_id
JOIN applications ON applications.posting_id = application_events.posting_id
WHERE application_events.stage IS NOT NULL
  AND applications.status NOT IN ('rejected', 'withdrawn')
"""


def stages_between(start, end):
    """[{event_id, id, company, title, url, status, stage, at, note, created_at,
    updated_at}] for the stages at or after start and before end, datetimes, soonest
    first, leaving out those of rejected and withdrawn applications."""
    rows = connect().execute(
        _STAGE_ROWS + "AND application_events.at >= ? AND application_events.at < ? "
        "ORDER BY application_events.at, application_events.id",
        (start.isoformat(timespec="seconds"), end.isoformat(timespec="seconds")))
    return [dict(row) for row in rows]


def upcoming_stages(days=7):
    """stages_between now and `days` days from now."""
    now = datetime.datetime.now()
    return stages_between(now, now + datetime.timedelta(days=days))


def past_due_stages(before=None):
    """The stages, in stages_between's shape, whose time is earlier than before, a
    datetime that defaults to now, with no later event on their posting, oldest
    first."""
    before = _now() if before is None else before.isoformat(timespec="seconds")
    rows = connect().execute(
        _STAGE_ROWS + "AND application_events.at < ? AND NOT EXISTS ("
        "SELECT 1 FROM application_events later "
        "WHERE later.posting_id = application_events.posting_id "
        "AND (later.at > application_events.at OR (later.at = application_events.at "
        "AND later.id > application_events.id))) "
        "ORDER BY application_events.at, application_events.id", (before,))
    return [dict(row) for row in rows]


def last_events():
    """posting id -> the time of its latest event, for every posting that has one."""
    return dict(connect().execute("SELECT posting_id, max(at) FROM application_events "
                                  "WHERE at IS NOT NULL GROUP BY posting_id").fetchall())


# --------------------------------------------------------------------------
# Checklists
# --------------------------------------------------------------------------
_ITEM = "SELECT id, posting_id, label, done, position FROM checklist "


def _item(row):
    return {**dict(row), "done": bool(row["done"])}


def _label(label):
    label = (label or "").strip() if isinstance(label, str) else ""
    if not label:
        raise ValueError("a checklist item needs a label")
    return label


def checklist(posting_id):
    rows = connect().execute(_ITEM + "WHERE posting_id = ? ORDER BY position, id",
                             (posting_id,))
    return [_item(row) for row in rows]


def checklist_item(item_id):
    row = connect().execute(_ITEM + "WHERE id = ?", (item_id,)).fetchone()
    return _item(row) if row else None


def add_item(posting_id, label):
    """Append an item to a posting's checklist. Raises ValueError for a blank label."""
    label = _label(label)
    conn = connect()
    with conn:
        cur = conn.execute(
            "INSERT INTO checklist (posting_id, label, done, position, created_at) "
            "SELECT ?, ?, 0, coalesce(max(position) + 1, 0), ? FROM checklist "
            "WHERE posting_id = ?", (posting_id, label, _now(), posting_id))
    return checklist_item(cur.lastrowid)


def set_item(item_id, label=None, done=None):
    """Change what is given of an item. Returns it, or None for no such item.

    Raises ValueError for a blank label.
    """
    changes = {}
    if label is not None:
        changes["label"] = _label(label)
    if done is not None:
        changes["done"] = int(bool(done))
    if changes:
        conn = connect()
        with conn:
            conn.execute(f"UPDATE checklist SET "
                         f"{', '.join(f'{name} = ?' for name in changes)} WHERE id = ?",
                         [*changes.values(), item_id])
    return checklist_item(item_id)


def delete_item(item_id):
    """Delete an item. Returns its posting id, or None for no such item."""
    found = checklist_item(item_id)
    if found:
        conn = connect()
        with conn:
            conn.execute("DELETE FROM checklist WHERE id = ?", (item_id,))
    return found and found["posting_id"]


def reorder(posting_id, ids):
    """Put a posting's checklist in the order of ids and return it.

    Raises ValueError unless ids holds each of its item ids exactly once.
    """
    ids = list(ids)
    conn = connect()
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        held = [row[0] for row in conn.execute("SELECT id FROM checklist WHERE posting_id = ?",
                                               (posting_id,))]
        if len(ids) != len(held) or set(ids) != set(held):
            raise ValueError("the order must list every item of the checklist once")
        conn.executemany("UPDATE checklist SET position = ? WHERE id = ?",
                         [(position, item_id) for position, item_id in enumerate(ids)])
    return checklist(posting_id)


# --------------------------------------------------------------------------
# Attachments
# --------------------------------------------------------------------------
_ATTACHMENT = "SELECT id, posting_id, label, path, added_at FROM attachments "


def _under_home(real):
    """Whether an ancestor of real is the home folder. The comparison goes through
    the file system, which on a case-insensitive volume matches /USERS/x to /Users/x."""
    home = os.path.realpath(Path.home())
    for parent in real.parents:
        try:
            if os.path.samefile(parent, home):
                return True
        except OSError:
            continue
    return False


def _home_file(path):
    """path with ~ expanded and symlinks resolved, when that names an existing regular
    file under the user's home directory. Raises ValueError otherwise.

    path is taken as given, so a file whose name ends in a space is found. Only when
    nothing exists there is path with its surrounding whitespace dropped tried."""
    if not isinstance(path, str) or not path.strip():
        raise ValueError("a file needs a path")
    given = Path(path).expanduser()
    if not given.exists():
        given = Path(path.strip()).expanduser()
    if not given.is_absolute():
        raise ValueError(f"{path} is not an absolute path")
    real = Path(os.path.realpath(given))
    if not _under_home(real):
        raise ValueError(f"{path} is outside your home folder")
    if not real.is_file():
        raise ValueError(f"{path} is not a file")
    return str(real)


def attachments(posting_id):
    rows = connect().execute(_ATTACHMENT + "WHERE posting_id = ? ORDER BY id", (posting_id,))
    return [dict(row) for row in rows]


def attachment(attachment_id):
    row = connect().execute(_ATTACHMENT + "WHERE id = ?", (attachment_id,)).fetchone()
    return dict(row) if row else None


def add_attachment(posting_id, label, path):
    """Record a file for a posting by its resolved path; a blank label takes the
    file's name. Raises ValueError unless the path is a file under the home folder."""
    real = _home_file(path)
    label = (label or "").strip() or Path(real).name
    conn = connect()
    with conn:
        cur = conn.execute("INSERT INTO attachments (posting_id, label, path, added_at) "
                           "VALUES (?, ?, ?, ?)", (posting_id, label, real, _now()))
    return attachment(cur.lastrowid)


def delete_attachment(attachment_id):
    """Forget a file; the file itself stays. Returns its posting id, or None."""
    found = attachment(attachment_id)
    if found:
        conn = connect()
        with conn:
            conn.execute("DELETE FROM attachments WHERE id = ?", (attachment_id,))
    return found and found["posting_id"]


# --------------------------------------------------------------------------
# Runs and counts
# --------------------------------------------------------------------------
def start_run(log_path=None):
    """Open a run row. A row still `running` belongs to a process that was killed."""
    conn = connect()
    with conn:
        conn.execute("UPDATE runs SET status = 'interrupted' WHERE status = 'running'")
        cur = conn.execute("INSERT INTO runs (started_at, status, log_path) "
                           "VALUES (?, 'running', ?)", (_now(), log_path))
    return cur.lastrowid


def finish_run(run_id, status, counts=None, log_start=None, log_end=None):
    conn = connect()
    with conn:
        conn.execute("UPDATE runs SET finished_at = ?, status = ?, counts = ?, "
                     "log_start = ?, log_end = ? WHERE id = ?",
                     (_now(), status, json.dumps(counts or {}, default=str),
                      log_start, log_end, run_id))


def set_log_end(run_id, log_end):
    conn = connect()
    with conn:
        conn.execute("UPDATE runs SET log_end = ? WHERE id = ?", (log_end, run_id))


def _run(row):
    return {**dict(row), "counts": json.loads(row["counts"]) if row["counts"] else None}


def list_runs(limit=10):
    rows = connect().execute("SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,))
    return [_run(row) for row in rows]


def relevant_ranked(cfg, run_ids):
    """Run id -> how many groups of the postings it scored pass relevant_query with a
    link not closed, for run_ids."""
    where, params = relevant_query(cfg)
    rows = connect().execute(
        "SELECT scores.run_id, count(DISTINCT coalesce(postings.group_key, postings.id)) "
        "FROM scores JOIN postings ON postings.id = scores.posting_id "
        f"WHERE {where} AND postings.link_status IS NOT 'closed' "
        "AND scores.run_id IN (SELECT value FROM json_each(?)) "
        "GROUP BY scores.run_id", params + [json.dumps(list(run_ids))])
    return dict(rows.fetchall())


def get_run(run_id):
    row = connect().execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
    return _run(row) if row else None


_COUNTS = {
    "postings": "SELECT count(*) FROM postings",
    "active": "SELECT count(*) FROM postings WHERE active = 1",
    "seen": "SELECT count(*) FROM seen",
    "scored": "SELECT count(*) FROM scores",
    "applied": ("SELECT count(*) FROM applications WHERE status IN ("
                + ", ".join(f"'{status}'" for status in SENT) + ")"),
    "runs": "SELECT count(*) FROM runs",
    "schema_version": "SELECT CAST(value AS INTEGER) FROM meta WHERE key = 'schema_version'",
}


def _latest_run(conn):
    row = conn.execute("SELECT id, started_at, finished_at, status, counts FROM runs "
                       "ORDER BY id DESC LIMIT 1").fetchone()
    if row is None:
        return None
    ranked = conn.execute("SELECT count(*) FROM scores WHERE run_id = ?",
                          (row["id"],)).fetchone()[0]
    return {**_run(row), "ranked": ranked}


def counts():
    """Sizes for `cairn status`, the schema version, postings per status, and
    the newest run with how many postings it scored.

    `active` counts postings whose feed marks them active AND that were present in
    their source's most recent fetch. `applied` counts postings in a SENT status.
    """
    conn = connect()
    return {**{name: conn.execute(sql).fetchone()[0] for name, sql in _COUNTS.items()},
            "applications": application_counts(), "latest_run": _latest_run(conn)}


# --------------------------------------------------------------------------
# One-time import of the state/*.json files that preceded this database
# --------------------------------------------------------------------------
def _text(value):
    """A scalar as text for a TEXT column; anything structured becomes None."""
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return None
    return str(value)


def _int_or_none(value):
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _list_or_empty(value):
    return [str(v) for v in value if v is not None] if isinstance(value, list) else []


def _day_timestamp(day):
    try:
        return datetime.datetime.fromisoformat(day).timestamp()
    except (TypeError, ValueError):
        return None


_LEGACY_POSTING = """
INSERT OR IGNORE INTO postings (id, source, company, title, url, url_key, locations,
                                category, active, visible, posted_at, first_seen_at,
                                last_seen_at)
VALUES (?, 'legacy', ?, ?, ?, ?, ?, ?, 1, 1, ?, ?, ?)
"""


def _legacy_posting(conn, posting_id, entry, locations, category, day):
    company, title, url = (_text(entry.get(k)) for k in ("company", "title", "url"))
    conn.execute(_LEGACY_POSTING, (
        posting_id, company, title, url,
        url_key({"url": url, "company_name": company, "title": title}),
        _json_list(locations), category, _day_timestamp(day), day, day))


def _import_postings(conn, postings, now):
    floor = settings.get().tier_floor
    for posting_id, p in postings.items():
        if not isinstance(p, dict):
            continue
        day = _text(p.get("day")) or now
        _legacy_posting(conn, posting_id, p, _list_or_empty(p.get("locations")),
                        _text(p.get("category")), day)
        fit, tier = _int_or_none(p.get("fit")), _int_or_none(p.get("tier"))
        if fit is not None:
            conn.execute("INSERT OR IGNORE INTO scores (posting_id, fit, tier, below_floor, "
                         "scored_at) VALUES (?, ?, ?, ?, ?)",
                         (posting_id, fit, tier,
                          None if tier is None else int(tier < floor), day))
    _refresh_fts(conn, postings)


def _import_applications(conn, applied, now):
    for posting_id, a in applied.items():
        if not isinstance(a, dict):
            continue
        _legacy_posting(conn, posting_id, a, None, None, now)
        applied_at = _text(a.get("applied_at")) or now
        cur = conn.execute(
            "INSERT OR IGNORE INTO applications (posting_id, status, applied_at, note, "
            "created_at, updated_at) VALUES (?, 'applied', ?, ?, ?, ?)",
            (posting_id, applied_at, _text(a.get("note")) or "", applied_at, applied_at))
        if cur.rowcount:
            conn.execute("INSERT INTO application_events (posting_id, status, at) "
                         "VALUES (?, 'applied', ?)", (posting_id, applied_at))
    _refresh_fts(conn, applied)


def _import_descriptions(conn, described, now):
    known = {row[0] for row in conn.execute("SELECT id FROM postings")}
    orphans = [i for i in described if i not in known]
    if orphans:
        ui.warn(f"[store] {len(orphans)} cached description(s) belong to postings "
                "no report ever listed; skipped them.")
    rows = [(i, _text(d.get("text")), _text(d.get("source")), _text(d.get("error")),
             _text(d.get("keywords")), _text(d.get("fetched")))
            for i, d in described.items() if i in known and isinstance(d, dict)]
    conn.executemany("INSERT OR IGNORE INTO descriptions (posting_id, text, source, error, "
                     "keywords, fetched_at) VALUES (?, ?, ?, ?, ?, ?)", rows)
    _refresh_fts(conn, [row[0] for row in rows])


def _import_seen(conn, seen, now):
    conn.executemany("INSERT OR IGNORE INTO seen VALUES (?, ?)",
                     [(i, now) for i in seen if isinstance(i, str) and i])


# postings first: applications may add postings, and descriptions need both
_LEGACY_FILES = (
    ("postings.json", dict, _import_postings),
    ("applications.json", dict, _import_applications),
    ("descriptions.json", dict, _import_descriptions),
    ("seen.json", list, _import_seen),
)


def _import_legacy_file(conn, path, kind, importer, now):
    """Import one file in its own transaction; a file that fails is skipped whole."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, kind):
            raise ValueError(f"expected a JSON {kind.__name__}, found {type(data).__name__}")
        with conn:
            importer(conn, data, now)
    except Exception as e:  # noqa: BLE001 - one broken legacy file must not block every command
        ui.warn(f"[store] skipped {path} ({type(e).__name__}: {e}). Later runs skip it too; "
                f"to retry, fix it and run: sqlite3 {paths.db_file()} "
                "\"DELETE FROM meta WHERE key = 'legacy_imported'\"")


def _import_legacy(conn, state_dir):
    state_dir = Path(state_dir)
    if not state_dir.is_dir():
        return False
    if conn.execute("SELECT 1 FROM meta WHERE key = 'legacy_imported'").fetchone():
        return False
    now = _now()
    for name, kind, importer in _LEGACY_FILES:
        if (state_dir / name).exists():
            _import_legacy_file(conn, state_dir / name, kind, importer, now)
    with conn:
        conn.execute("INSERT INTO meta VALUES ('legacy_imported', ?)", (now,))
    return True


def import_legacy(state_dir):
    """Copy state/*.json into the database once. Returns whether anything ran.

    The JSON files are left where they are; once imported they are never read again.
    """
    conn = connect()
    imported = _import_legacy(conn, state_dir)
    _sync_relevance(conn, settings.base())
    return imported
