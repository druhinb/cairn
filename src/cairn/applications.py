"""Track which postings you actually applied to."""
import datetime

from cairn import store


def record(job_id, note=""):
    """Mark a posting applied, or None if no stored posting matches the id (caller warns).

    An empty note keeps the stored one, so re-running `applied <id>` never erases it.
    """
    job_id = store.resolve(job_id)
    return store.set_status(job_id, "applied", note or None) if job_id else None


def annotate(results):
    """Flag postings you have applied to before — same posting, or same company."""
    sent = store.applications(store.SENT)
    by_id = {a["id"]: a for a in sent}
    # reversed so the most recently updated row for a company is the one kept
    by_company = {(a["company"] or "").casefold(): a for a in reversed(sent)}
    by_company.pop("", None)
    for r in results:
        prior = by_id.get(r.get("id")) or by_company.get((r.get("company_name") or "").casefold())
        if prior:
            r["repeat"] = True
            r["applied_before"] = {"company": prior["company"],
                                   "date": (prior["applied_at"] or "")[:10],
                                   "status": prior["status"]}
    return results


def stats(recent=10):
    """(applications sent, sent in the last 7 days, the N most recent as (id, row) pairs)."""
    sent = sorted(store.applications(store.SENT), key=lambda a: a["applied_at"] or "",
                  reverse=True)
    week_ago = (datetime.datetime.now() - datetime.timedelta(days=7)).isoformat()
    this_week = sum(1 for a in sent if (a["applied_at"] or "") >= week_ago)
    return len(sent), this_week, [(a["id"], a) for a in sent[:recent]]
