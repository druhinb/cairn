"""The database schema, and the migrations that bring an older database up to it."""
from pathlib import Path

from cairn.store.keys import _GROUP_COLUMNS, _assign_groups, url_key

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
