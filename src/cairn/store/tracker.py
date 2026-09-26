"""Applications, their status history, and interview stages."""
import datetime
import json

from cairn.store import db
from cairn.store.db import connect
from cairn.store.relevance import _marks
from cairn.store.rows import _list
from cairn.store.schema import DEFAULT_CHECKLIST, SENT, STAGES, STATUSES

_SET_STATUS = """
INSERT INTO applications (posting_id, status, applied_at, note, created_at, updated_at)
VALUES (:id, :status, :applied_at, :note, :now, :now)
ON CONFLICT(posting_id) DO UPDATE SET
    status = excluded.status,
    applied_at = coalesce(applications.applied_at, excluded.applied_at),
    note = coalesce(excluded.note, applications.note),
    updated_at = excluded.updated_at
"""

# db.now()'s format, which every stored time uses
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
        _change_status(conn, posting_id, status, note, db.now())
    return application(posting_id)


def set_note(posting_id, note):
    """Replace the note of a posting that has a status. Returns its row, or None."""
    conn = connect()
    with conn:
        cur = conn.execute("UPDATE applications SET note = ?, updated_at = ? "
                           "WHERE posting_id = ?", (note, db.now(), posting_id))
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
    """Every application row with its posting, most recently updated first."""
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


def _stage_name(stage):
    if stage not in STAGES:
        raise ValueError(f"unknown stage {stage!r}; expected one of {', '.join(STAGES)}")
    return stage


def _stage_time(at):
    """at, an ISO date or datetime, as a local time in db.now()'s format."""
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
    conn, now = connect(), db.now()
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
    changes["updated_at"] = db.now()
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
    now = datetime.datetime.now()
    return stages_between(now, now + datetime.timedelta(days=days))


def past_due_stages(before=None):
    """The stages, in stages_between's shape, whose time is earlier than before, a
    datetime that defaults to now, with no later event on their posting, oldest
    first."""
    before = db.now() if before is None else before.isoformat(timespec="seconds")
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
