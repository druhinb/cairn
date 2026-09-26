"""Setup from a resume: one Claude call drafts profile.md, and the answers to a few
questions become config.toml.

The draft holds only what the resume states. The answers write work authorization,
company anchors and locations into it.
"""
from cairn.ai.onboard.files import (
    DRAFT_FILES,
    PREFS_FILE,
    Applied,
    FilesExist,
    apply,
    conflicts,
    draft_dir,
    initialised,
    load_draft,
    needs_setup,
    save_draft,
    save_prefs,
)
from cairn.ai.onboard.preferences import (
    LOCATION_HEADING,
    MAX_ANCHORS,
    PREF_SHAPES,
    SETTING_FOR_PREF,
    SNAPSHOT_HEADING,
    TIER_HEADING,
    checked_prefs,
    default_prefs,
    preferences_to_settings,
    with_answers,
)
from cairn.ai.onboard.resume import (
    DRAFT_PROMPT,
    DRAFT_TIMEOUT,
    ENVELOPE,
    MARKERS,
    MAX_RESUME_CHARS,
    PDFTOTEXT_TIMEOUT,
    RESUME_DELIMITERS,
    ROLE_KEYWORDS,
    ROLE_UNSKIPS,
    SECTION_GUIDE,
    SUGGESTION_SHAPES,
    TITLE_DEFAULTS,
    WORK_AUTHORIZATION,
    ClaudeFailed,
    Draft,
    OnboardError,
    draft_from_resume,
    draft_prompt,
    required_headings,
    resume_text,
)
