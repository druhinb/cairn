"""What ranking reads and writes: seen postings, scores, descriptions, and feedback."""
from cairn.store import db
from cairn.store.db import connect
from cairn.store.postings import _refresh_fts

FEEDBACK_REASONS = ("location", "compensation", "role", "company", "seniority", "other")


def mark_seen(ids):
    """Record ids as seen. Returns the new total."""
    conn = connect()
    now = db.now()
    with conn:
        conn.executemany("INSERT OR IGNORE INTO seen VALUES (?, ?)",
                         [(i, now) for i in ids if i])
    return conn.execute("SELECT count(*) FROM seen").fetchone()[0]


def seen_ids():
    return {row[0] for row in connect().execute("SELECT posting_id FROM seen")}


def is_seen(posting_id):
    return connect().execute("SELECT 1 FROM seen WHERE posting_id = ?",
                             (posting_id,)).fetchone() is not None


def save_scores(results, run_id=None):
    now = db.now()
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
                     (posting_id, text, source, error, keywords, db.now()))
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
            "id": posting_id, "keywords": keywords, "now": db.now(), "min": salary.get("min"),
            "max": salary.get("max"), "period": salary.get("period"),
            "currency": salary.get("currency"),
            "sponsorship": None if sponsorship is None else ("yes" if sponsorship else "no")})
        _refresh_fts(conn, [posting_id])


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
                     (posting_id, verdict, reason, note, db.now()))
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
