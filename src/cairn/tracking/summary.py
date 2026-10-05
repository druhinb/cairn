"""The Today summary: what changed since the last visit, and what to do next."""
import datetime

from cairn import store
from cairn.core import settings
from cairn.jobs import insights
from cairn.tracking import applications
from cairn.tracking.followups import stale_applications

TODAY_PICKS = 5
PICKS_DAYS = 14
# the strong matches found in the last day and posted in the last three come first,
# then those posted in the last week, then the newest run's best
FRESH_PICKS = ({"found_within_days": 1, "posted_within_days": 3}, {"posted_within_days": 7})
TODAY_SKILLS = 5
SKILLS_FROM = 100
INTERVIEW_DAYS = 7
CLOSED_DAYS = 7
COST_DAYS = 30


def _new_since(since):
    """{count, run_id}: the relevant groups ranked by the runs that finished after
    since, a datetime, or by the latest run when since is None, and the newest of
    those runs."""
    conn = store.connect()
    if since is None:
        rows = conn.execute("SELECT id FROM runs WHERE status = 'ok' ORDER BY id DESC LIMIT 1")
    else:
        rows = conn.execute("SELECT id FROM runs WHERE status = 'ok' AND finished_at > ? "
                            "ORDER BY id DESC", (since.isoformat(timespec="seconds"),))
    run_ids = [row[0] for row in rows]
    ranked = store.relevant_ranked(settings.get(), run_ids) if run_ids else {}
    return {"count": sum(ranked.values()), "run_id": run_ids[0] if run_ids else None}


def _picks():
    """Up to TODAY_PICKS best-scored postings with no status and a link not found
    closed, in store.search's shape: the FRESH_PICKS scopes at fit_threshold or
    above first, then the newest run that scored any. Scores stored before runs
    were numbered belong to no run, so those fall back to the best-scored recent
    postings."""
    run_id = store.connect().execute("SELECT max(run_id) FROM scores").fetchone()[0]
    fit_min = settings.get().fit_threshold
    scopes = [{**scope, "fit_min": fit_min} for scope in FRESH_PICKS]
    scopes.append({"run_id": run_id} if run_id is not None else {"posted_within_days": PICKS_DAYS})
    picks = {}
    for scope in scopes:
        # the score sort puts closed links last, so dropping them from the top page
        # leaves the best open ones
        for row in store.search(status=["none"], limit=TODAY_PICKS, **scope)[0]:
            if not row["closed"] and row["fit"] is not None:
                picks.setdefault(row["id"], row)
        if len(picks) >= TODAY_PICKS:
            break
    return list(picks.values())[:TODAY_PICKS]


def _closed_recently():
    since = datetime.datetime.now() - datetime.timedelta(days=CLOSED_DAYS)
    return store.connect().execute(
        "SELECT count(DISTINCT coalesce(postings.group_key, postings.id)) FROM postings "
        "JOIN scores ON scores.posting_id = postings.id "
        "WHERE postings.link_status = 'closed' AND postings.link_checked_at >= ?",
        (since.isoformat(timespec="seconds"),)).fetchone()[0]


def today(since=None):
    """The Today summary. picks hold search rows; since, a datetime, is when the
    user last looked, which new_since_last_visit counts from."""
    interviews = [{key: row[key] for key in ("event_id", "id", "company", "title", "stage",
                                             "at", "note")}
                  for row in store.upcoming_stages(INTERVIEW_DAYS)]
    skills = [row for row in insights.skills_gap(SKILLS_FROM) if row["missing_in"]]
    return {"new_since_last_visit": _new_since(since), "picks": _picks(),
            "follow_ups": stale_applications(), "interviews": interviews,
            "applied_this_week": applications.stats()[1],
            "closed_recently": _closed_recently(), "skills": skills[:TODAY_SKILLS],
            "cost": store.call_counts(COST_DAYS)}
