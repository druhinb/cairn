"""One-time import of the state/*.json files that preceded this database."""
import datetime
import json
from pathlib import Path

from cairn.core import paths, settings, ui
from cairn.store import db
from cairn.store.db import connect
from cairn.store.keys import url_key
from cairn.store.postings import _refresh_fts
from cairn.store.relevance import _sync_relevance
from cairn.store.rows import _json_list


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
    now = db.now()
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
