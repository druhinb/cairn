"""The answers to the setup questions, as settings and as lines in profile.md."""
import json
import re

from cairn.ai.onboard.resume import (
    TITLE_DEFAULTS,
    WORK_AUTHORIZATION,
    OnboardError,
    _int_or_none,
    _str_list,
    title_words,
)
from cairn.core import settings
from cairn.sources.watchlists import STARTERS

MAX_ANCHORS = 3

# pref key -> the setting it becomes unchanged; title lists and locations are derived
SETTING_FOR_PREF = {
    "job_type": "job_type",
    "graduation_year": "graduation_year",
    "degrees_held": "degrees_held",
    "internship_terms": "wanted_intern_terms",
    "ntfy_topic": "notify_ntfy_topic",
    "experience_years": "experience_years",
    "max_years_required": "max_years_required",
    "us_only": "us_only",
    "recent_days": "recent_days",
}

PREF_SHAPES = {
    **{key: (_str_list, "a list of strings") for key in TITLE_DEFAULTS},
    "locations": (_str_list, "a list of strings"),
    "remote_ok": (lambda v: isinstance(v, bool), "true or false"),
    "us_only": (lambda v: isinstance(v, bool), "true or false"),
    "job_type": (lambda v: v in settings.JOB_TYPES, f"one of {', '.join(settings.JOB_TYPES)}"),
    "graduation_year": (_int_or_none, "an integer year or null"),
    "degrees_held": (_str_list, "a list of strings"),
    "internship_terms": (_str_list, "a list of strings"),
    "work_authorization": (lambda v: v in WORK_AUTHORIZATION,
                           f"one of {', '.join(WORK_AUTHORIZATION)}"),
    "calibre_anchors": (lambda v: _str_list(v) and len(v) <= MAX_ANCHORS,
                        f"a list of up to {MAX_ANCHORS} strings"),
    "ntfy_topic": (lambda v: isinstance(v, str), "a string"),
    "experience_years": (lambda v: type(v) is int and 0 <= v <= 50, "a whole number from 0 to 50"),
    "max_years_required": (lambda v: v is None or (type(v) is int and 0 <= v <= 50),
                           "a whole number from 0 to 50, or null"),
    "recent_days": (lambda v: type(v) is int and 1 <= v <= 365, "a whole number from 1 to 365"),
    "watchlist": (_str_list, "a list of strings"),
    "starter_watchlists": (lambda v: _str_list(v) and set(v) <= set(STARTERS),
                           f"a list drawn from {', '.join(STARTERS)}"),
    "overwrite": (lambda v: isinstance(v, bool), "true or false"),
}


def checked_prefs(prefs):
    """prefs with blanks trimmed, or OnboardError naming the first bad key."""
    if not isinstance(prefs, dict):
        raise OnboardError("preferences should be an object")
    for key, value in prefs.items():
        if key not in PREF_SHAPES:
            raise OnboardError(f"unknown preference '{key}'")
        fits, expected = PREF_SHAPES[key]
        if not fits(value):
            raise OnboardError(f"preference '{key}' should be {expected}, "
                               f"got {json.dumps(value)[:80]}")
    return {key: [v.strip() for v in value if v.strip()] if _str_list(value) else value
            for key, value in prefs.items()}


def _title_settings(prefs):
    changes = {key: title_words(prefs[key]) for key in TITLE_DEFAULTS if key in prefs}
    # an empty title_keywords would hide every posting
    if changes.get("title_keywords") == []:
        changes["title_keywords"] = list(settings.DEFAULT_TITLE_KEYWORDS)
    return changes


def _location_allow(locations, remote_ok):
    allow = list(locations)
    # an empty list already keeps every location, remote ones included
    if remote_ok and allow and not any(place.lower() == "remote" for place in allow):
        allow.append("Remote")
    return allow


def _mapped(prefs, base):
    changes = {SETTING_FOR_PREF[key]: value for key, value in prefs.items()
               if key in SETTING_FOR_PREF}
    changes.update(_title_settings(prefs))
    if "locations" in prefs or "remote_ok" in prefs:
        changes["location_allow"] = _location_allow(
            prefs.get("locations", base.location_allow), prefs.get("remote_ok", False))
    return settings.from_dict(changes, base=base, source="preferences")


def preferences_to_settings(prefs, base):
    """base with the settings the answers in prefs imply. Keys left out keep base's."""
    return _mapped(checked_prefs(prefs), base)


def default_prefs(suggestions):
    """Answers to start from, taken from the draft's suggestions."""
    return {**{key: suggestions.get(key, list(default)) for key, default in TITLE_DEFAULTS.items()},
            "locations": suggestions.get("locations", []),
            "remote_ok": False,
            "job_type": suggestions.get("job_type", "both"),
            "graduation_year": suggestions.get("graduation_year"),
            "degrees_held": suggestions.get("degrees_held", []),
            "internship_terms": suggestions.get("internship_terms", []),
            "work_authorization": suggestions.get("work_authorization", "unknown"),
            "calibre_anchors": [],
            "recent_days": settings.base().recent_days,
            "ntfy_topic": "",
            "watchlist": []}


_AUTHORIZATION_LINE = re.compile(r"work authori[sz]ation", re.I)
_STATED = "  Stated during setup:"
SNAPSHOT_HEADING = "Snapshot"
TIER_HEADING = "Company tier (drives the `tier` score, 0-100)"
LOCATION_HEADING = "Location preference (soft, in priority order)"


def _section(lines, heading):
    """(start, end) of a `## heading` section's lines less its trailing blank lines,
    or None when there is no such section."""
    start = next((i for i, line in enumerate(lines)
                  if line.startswith("## ") and line[3:].strip() == heading), None)
    if start is None:
        return None
    end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("## ")),
               len(lines))
    while end > start + 1 and not lines[end - 1].strip():
        end -= 1
    return start, end


def _append_to_section(text, heading, bullets):
    lines = text.rstrip("\n").splitlines()
    bounds = _section(lines, heading)
    if bounds is None:
        return "\n".join(lines + ["", f"## {heading}", *bullets]) + "\n"
    lines[bounds[1]:bounds[1]] = bullets
    return "\n".join(lines) + "\n"


def _with_authorization(text, authorization):
    line = f"- Work authorization: {authorization}."
    lines = text.rstrip("\n").splitlines()
    for i, existing in enumerate(lines):
        if _AUTHORIZATION_LINE.search(existing):
            # the answer from an earlier setup gives way to this one
            stated = i + 1 < len(lines) and lines[i + 1].startswith(_STATED)
            if existing == line and not stated:
                return text
            lines[i + 1:i + 1 + stated] = [f"{_STATED} {authorization}."]
            return "\n".join(lines) + "\n"
    return _append_to_section(text, SNAPSHOT_HEADING, [line])


def _with_locations(text, locations, remote_ok):
    places = _location_allow(locations, remote_ok)
    lines = text.rstrip("\n").splitlines()
    bounds = _section(lines, LOCATION_HEADING)
    if bounds is not None:
        del lines[bounds[0] + 1:bounds[1]]
    return _append_to_section("\n".join(lines), LOCATION_HEADING, [f"- {', '.join(places)}."])


def with_answers(profile_md, prefs):
    """profile.md with the stated work authorization, calibre anchors and locations
    written in."""
    text = profile_md
    if prefs.get("work_authorization", "unknown") != "unknown":
        text = _with_authorization(text, prefs["work_authorization"])
    if prefs.get("calibre_anchors"):
        text = _append_to_section(text, TIER_HEADING,
                                  [f"- {anchor}" for anchor in prefs["calibre_anchors"]])
    if prefs.get("locations"):
        text = _with_locations(text, prefs["locations"], prefs.get("remote_ok", False))
    return text
