"""The years of experience a posting asks for, read from its description.

A company board lists every opening, senior ones included, under titles such as
"Software Engineer, GenAI Platform", so the title rules pass them. The description
says how many years it wants, and a posting asking for more than
max_years_required is left out before ranking.
"""
import re
import time

from cairn import store
from cairn.core import events, settings
from cairn.jobs import descriptions, fetch
from cairn.sources.web import _pooled

WORKERS = 8
DEADLINE_SECONDS = 600
# ranked postings read per run whose years were never read
BACKLOG_PER_RUN = 500

_NUMBER = {"zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
           "seven": 7, "eight": 8, "nine": 9, "ten": 10}
_YEARS = re.compile(
    r"\b(\d{1,2}|" + "|".join(_NUMBER) + r")\s*\+?\s*"
    r"(?:(?:-|–|to|or more)\s*(?:\d{1,2}\s*)?\+?\s*)?(?:plus\s+)?years?\b"
    # what follows the years, up to the word experience
    r"(?P<between>[^.;\n]{0,60}?)\bexperience", re.I)
# years that measure schooling or an age
_NOT_WORK = re.compile(r"degree|old|program|school|college|university|phd|study", re.I)


def required_years(text):
    """The fewest years of experience text asks for, or None when it states none."""
    found = [int(m[1]) if m[1].isdigit() else _NUMBER[m[1].lower()]
             for m in _YEARS.finditer(text or "") if not _NOT_WORK.search(m["between"])]
    return min(found) if found else None


def read(jobs):
    """Read each job's description, fetching the ones not stored yet, and store the
    years it asks for. Sets years_required on each job dict read by the deadline."""
    if not jobs:
        return
    by_id = {job["id"]: job for job in jobs}
    done = _pooled(lambda posting_id: required_years(descriptions.get(by_id[posting_id])),
                   list(by_id), WORKERS, time.monotonic() + DEADLINE_SECONDS)
    years = {posting_id: result for posting_id, (result, error) in done.items() if error is None}
    for posting_id, n in years.items():
        by_id[posting_id]["years_required"] = n
    store.set_years_required(years)


def within_limit(new):
    """new less the full-time postings whose description asks for more years than
    max_years_required. The descriptions stay stored for the summaries."""
    most = settings.get().max_years_required
    if most is None:
        return new
    read([job for job in new if not fetch.is_internship(job)])
    kept = [job for job in new if (job.get("years_required") or 0) <= most
            or fetch.is_internship(job)]
    if len(kept) < len(new):
        events.emit("info", text=f"[experience] left out {len(new) - len(kept)} posting(s) "
                                 f"asking for more than {most} years")
    return kept


def read_ranked(limit=BACKLOG_PER_RUN):
    """Read the years of ranked postings from before descriptions were read for them,
    so a limit set later hides them too."""
    if settings.get().max_years_required is None:
        return
    jobs = store.ranked_without_years(limit)
    store.set_years_required({job["id"]: None for job in jobs if fetch.is_internship(job)})
    read([job for job in jobs if not fetch.is_internship(job)])
