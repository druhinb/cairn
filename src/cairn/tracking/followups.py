"""Sent applications that have gone quiet long enough to follow up on."""
import datetime

from cairn import store
from cairn.core import settings

# days a passed interview stage may go without a later event before it needs a follow-up
STAGE_GRACE_DAYS = 3


def _when(text):
    """text, an ISO date or time, as a naive local datetime, or None for text that
    is not one. Legacy imports stored applied_at as whatever text they held."""
    try:
        when = datetime.datetime.fromisoformat(text)
        return when.astimezone().replace(tzinfo=None) if when.tzinfo else when
    except (TypeError, ValueError, OverflowError):
        return None


def _follow_up(row, last_event_at, when, now):
    return {"id": row["id"], "company": row["company"], "title": row["title"],
            "status": row["status"], "applied_at": row["applied_at"],
            "days_since": (now - when).days, "last_event_at": last_event_at}


def stale_applications(days=None):
    """[{id, company, title, status, applied_at, days_since, last_event_at}], longest
    waiting first: applications in status applied whose latest event is `days` or
    more days old, follow_up_days by default, and interviewing ones whose latest
    event is a stage more than STAGE_GRACE_DAYS past. An application whose stored
    time is not ISO is left out."""
    if days is None:
        days = settings.get().follow_up_days
    now = datetime.datetime.now()
    cutoff = now - datetime.timedelta(days=days)
    last = store.last_events()
    stale = []
    for row in store.applications(["applied"]):
        at = last.get(row["id"]) or row["applied_at"] or row["updated_at"]
        when = _when(at)
        if when is not None and when <= cutoff:
            stale.append(_follow_up(row, at, when, now))
    interviewing = {row["id"]: row for row in store.applications(["interviewing"])}
    grace = now - datetime.timedelta(days=STAGE_GRACE_DAYS)
    for due in store.past_due_stages(grace):
        if due["id"] in interviewing:
            when = datetime.datetime.fromisoformat(due["at"])
            stale.append(_follow_up(interviewing[due["id"]], due["at"], when, now))
    return sorted(stale, key=lambda r: (-r["days_since"], r["company"] or "", r["id"]))


def follow_up_note(rows):
    """One line for a push naming how many applications need a follow-up and the
    first two companies, or "" for none."""
    if not rows:
        return ""
    head = (f"{len(rows)} application needs a follow-up" if len(rows) == 1
            else f"{len(rows)} applications need a follow-up")
    names = list(dict.fromkeys(row["company"] for row in rows if row["company"]))[:2]
    return f"{head}: {', '.join(names)}" if names else head
