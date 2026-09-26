"""Runs, their counts, and one row per Claude call."""
import datetime
import json

from cairn.store import db
from cairn.store.db import connect
from cairn.store.relevance import relevant_query
from cairn.store.schema import SENT
from cairn.store.tracker import application_counts

CALL_KINDS = ("rank", "summary", "onboard")


def record_call(kind, model, prompt_chars, output_chars, run_id=None):
    conn = connect()
    with conn:
        conn.execute("INSERT INTO claude_calls (made_at, kind, model, prompt_chars, "
                     "output_chars, run_id) VALUES (?, ?, ?, ?, ?, ?)",
                     (db.now(), kind, model, prompt_chars, output_chars, run_id))


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


def start_run(log_path=None):
    """Open a run row. A row still `running` belongs to a process that was killed."""
    conn = connect()
    with conn:
        conn.execute("UPDATE runs SET status = 'interrupted' WHERE status = 'running'")
        cur = conn.execute("INSERT INTO runs (started_at, status, log_path) "
                           "VALUES (?, 'running', ?)", (db.now(), log_path))
    return cur.lastrowid


def finish_run(run_id, status, counts=None, log_start=None, log_end=None):
    conn = connect()
    with conn:
        conn.execute("UPDATE runs SET finished_at = ?, status = ?, counts = ?, "
                     "log_start = ?, log_end = ? WHERE id = ?",
                     (db.now(), status, json.dumps(counts or {}, default=str),
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
