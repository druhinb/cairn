"""What an application needs next: follow-ups that are due, the Today summary, and
interview stages as a calendar feed."""
from cairn.tracking.calendar_feed import (
    CALENDAR_DAYS_AHEAD,
    CALENDAR_DAYS_BACK,
    EVENT_LENGTH,
    LINE_OCTETS,
    STAGE_LABELS,
    ics,
)
from cairn.tracking.followups import (
    STAGE_GRACE_DAYS,
    follow_up_note,
    stale_applications,
)
from cairn.tracking.summary import (
    CLOSED_DAYS,
    COST_DAYS,
    INTERVIEW_DAYS,
    PICKS_DAYS,
    SKILLS_FROM,
    TODAY_PICKS,
    TODAY_SKILLS,
    today,
)
