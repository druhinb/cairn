"""What an application needs next: follow-ups that are due, the Today summary, and
interview stages as a calendar feed."""
import calendar
import datetime
import re
import time

from cairn import applications, store
from cairn.core import settings
from cairn.jobs import insights

STAGE_LABELS = {"screen": "Screen", "oa": "Online assessment", "onsite": "Onsite",
                "final": "Final round", "other": "Interview"}

# days a passed interview stage may go without a later event before it needs a follow-up
STAGE_GRACE_DAYS = 3
TODAY_PICKS = 5
PICKS_DAYS = 14
TODAY_SKILLS = 5
SKILLS_FROM = 100
INTERVIEW_DAYS = 7
CLOSED_DAYS = 7
COST_DAYS = 30
EVENT_LENGTH = datetime.timedelta(hours=1)
CALENDAR_DAYS_BACK = 30
CALENDAR_DAYS_AHEAD = 90
# RFC 5545 3.1: content lines longer than 75 octets are folded
LINE_OCTETS = 75


def _when(text):
    """text, an ISO date or time, as a naive local datetime, or None for text that
    is not one. Legacy imports stored applied_at as whatever text they held."""
    try:
        when = datetime.datetime.fromisoformat(text)
        return when.astimezone().replace(tzinfo=None) if when.tzinfo else when
    except (TypeError, ValueError, OverflowError):
        return None


# --------------------------------------------------------------------------
# Follow-ups
# --------------------------------------------------------------------------
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


# --------------------------------------------------------------------------
# Today
# --------------------------------------------------------------------------
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
    closed, from the newest run that scored any, in store.search's shape. Scores
    stored before runs were numbered belong to no run, so those fall back to the
    best-scored recent postings."""
    run_id = store.connect().execute("SELECT max(run_id) FROM scores").fetchone()[0]
    scope = {"run_id": run_id} if run_id is not None else {"posted_within_days": PICKS_DAYS}
    # the score sort puts closed links last, so dropping them from the top page
    # leaves the best open ones
    rows = store.search(status=["none"], limit=TODAY_PICKS, **scope)[0]
    return [row for row in rows if not row["closed"] and row["fit"] is not None]


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


# --------------------------------------------------------------------------
# Calendar
# --------------------------------------------------------------------------
def _utc(when):
    """A local naive datetime as an RFC 5545 UTC DATE-TIME. A time a DST change
    repeats reads as its first occurrence, and one it skips with the offset before
    the change."""
    if when.tzinfo is None:
        when = datetime.datetime.fromtimestamp(_local_instant(when), datetime.timezone.utc)
    return when.astimezone(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _local_instant(naive):
    """The epoch second at which the system clock reads `naive`. Python 3.11's
    astimezone() reads a skipped time with the offset after the change, 3.12 and
    later with the one before, so the offsets come from localtime(), which only
    ever converts from UTC."""
    clock = calendar.timegm(naive.timetuple())
    before = time.localtime(clock - 86400).tm_gmtoff
    after = time.localtime(clock + 86400).tm_gmtoff
    # only a time past the change reads back under the later offset alone
    if (time.localtime(clock - after).tm_gmtoff == after
            and time.localtime(clock - before).tm_gmtoff != before):
        return clock - after
    return clock - before


# RFC 5545 3.3.11: TEXT excludes the control characters, newline aside
_CONTROL = re.compile(r"[\x00-\x09\x0b-\x1f\x7f]")


def _text(value):
    """value as an RFC 5545 TEXT value, each control character but newline a space."""
    value = value.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,")
    value = _CONTROL.sub(" ", value.replace("\r\n", "\n").replace("\r", "\n"))
    return value.replace("\n", "\\n")


def _fold(line):
    """line split into LINE_OCTETS-octet pieces, never inside a UTF-8 character, each
    after the first led by the space that marks a continuation."""
    pieces, piece, size, limit = [], "", 0, LINE_OCTETS
    for char in line:
        width = len(char.encode())
        if size + width > limit:
            pieces.append(piece)
            piece, size, limit = "", 0, LINE_OCTETS - 1
        piece += char
        size += width
    pieces.append(piece)
    return "\r\n ".join(pieces)


def _epoch(text):
    return int(datetime.datetime.fromisoformat(text).timestamp())


def _vevent(row, stamp):
    """A stage's VEVENT. Its UID pairs the row id with its creation second because a
    deleted row's id can be given to the next stage; SEQUENCE counts the seconds from
    creation to the last change, so each edit raises it."""
    begins = datetime.datetime.fromisoformat(row["at"])
    created = _epoch(row["created_at"])
    summary = f"{row['company'] or 'Application'} · {STAGE_LABELS[row['stage']]}"
    description = "\n\n".join(part for part in (row["note"], store.web_url(row["url"]))
                              if part)
    lines = ["BEGIN:VEVENT", f"UID:stage-{row['event_id']}-{created}@cairn",
             f"SEQUENCE:{_epoch(row['updated_at']) - created}",
             f"DTSTAMP:{stamp}", f"DTSTART:{_utc(begins)}",
             f"DTEND:{_utc(begins + EVENT_LENGTH)}", f"SUMMARY:{_text(summary)}"]
    if description:
        lines.append(f"DESCRIPTION:{_text(description)}")
    return lines + ["END:VEVENT"]


def ics(days_ahead=CALENDAR_DAYS_AHEAD):
    """An RFC 5545 calendar with one hour-long event per stage from the start of the
    day CALENDAR_DAYS_BACK days ago through days_ahead days from now, so a
    subscription keeps recent interviews."""
    now = datetime.datetime.now()
    start = (now - datetime.timedelta(days=CALENDAR_DAYS_BACK)).replace(
        hour=0, minute=0, second=0, microsecond=0)
    stamp = _utc(now)
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//cairn//tracking//EN",
             "CALSCALE:GREGORIAN", "METHOD:PUBLISH"]
    for row in store.stages_between(start, now + datetime.timedelta(days=days_ahead)):
        lines += _vevent(row, stamp)
    lines.append("END:VCALENDAR")
    return "".join(_fold(line) + "\r\n" for line in lines)
