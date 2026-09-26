"""Interview stages as an iCalendar feed."""
import calendar
import datetime
import re
import time

from cairn import store

STAGE_LABELS = {"screen": "Screen", "oa": "Online assessment", "onsite": "Onsite",
                "final": "Final round", "other": "Interview"}

EVENT_LENGTH = datetime.timedelta(hours=1)
CALENDAR_DAYS_BACK = 30
CALENDAR_DAYS_AHEAD = 90
# RFC 5545 3.1: content lines longer than 75 octets are folded
LINE_OCTETS = 75


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
