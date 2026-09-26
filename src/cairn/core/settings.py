"""Tunables, read from ~/.cairn/config.toml.

Every setting has a default that works; config.toml only carries the ones a user
changed. `get()` is what every module reads from: the process-wide `base()`, unless
`override()` has replaced it for one run (the `--fit` flag).
`use()` replaces the base, after an edit in the web app or for one test.
"""
import contextvars
import dataclasses
import json
import tomllib
import types
import typing
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from cairn import sources
from cairn.core import events, paths


class SettingsError(Exception):
    """A config.toml that cannot be trusted: unknown key, wrong type, bad TOML."""


# The store dedupes postings across sources by apply URL, and the first source to
# list one keeps it, so the feeds that carry a category come first. The filter drops
# nearly every posting in the Summer2027 internship feeds as a summer one; this list
# carries them for the handful of off-season postings wanted_intern_terms lets through.
DEFAULT_SOURCES = [
    {"kind": "github", "location": "https://raw.githubusercontent.com/SimplifyJobs/"
                                   "New-Grad-Positions/dev/.github/scripts/listings.json"},
    {"kind": "github", "location": "https://raw.githubusercontent.com/SimplifyJobs/"
                                   "Summer2027-Internships/dev/.github/scripts/listings.json"},
    {"kind": "github", "location": "https://raw.githubusercontent.com/vanshb03/"
                                   "New-Grad-2026/dev/.github/scripts/listings.json"},
    {"kind": "github", "location": "https://raw.githubusercontent.com/vanshb03/"
                                   "Summer2027-Internships/dev/.github/scripts/listings.json"},
    {"kind": "github_readme", "location": "https://raw.githubusercontent.com/speedyapply/"
                                          "2027-SWE-College-Jobs/main/NEW_GRAD_USA.md"},
]

DEFAULT_ALLOWED_CATEGORIES = [
    "Software", "Software Engineering",
    "AI/ML/Data", "Data Science, AI & Machine Learning",
    "Quant", "Quantitative Finance",
]

DEFAULT_TITLE_KEYWORDS = [
    "software engineer", "software developer", "swe", "sde", "engineer", "developer",
    "programmer", "quant", "trader", "trading", "research scientist", "research engineer",
    "machine learning", "ml engineer", "data scientist", "forward deployed", "fdse",
    "solutions engineer", "platform", "infrastructure", "distributed", "compiler",
    "backend", "frontend", "front end", "full stack", "fullstack", "systems engineer",
    "site reliability", "sre",
]

DEFAULT_TITLE_EXCLUDE = ["senior", "sr.", "staff", "principal", "lead", "manager",
                         "director", "phd"]

DEFAULT_TITLE_EXCLUDE_FIELD = [
    "mechanical", "civil", "electrical", "chemical", "biomedical", "aerospace",
    "structural", "industrial engineer", "manufacturing", "process engineer",
    "environmental", "geotechnical", "packaging", "photonics", "optical", "analog",
    "firmware", "fpga", "hardware", "asic", "silicon", "failure analysis",
    "field engineer", "field service", "application engineer", "application analyst",
    "test engineer", "validation engineer", "support engineer", "quality",
    "drafter", "technician", "meteorolog", "facilities", "sales", "marketing",
    "recruit", "account manager",
]

DEFAULT_INTERN_TERMS = ["intern", "internship", "co-op", "coop"]

@dataclass
class Settings:
    # Each entry holds the sources.Source fields kind and location, and optionally
    # company and enabled. watchlist takes company boards only, fetched after sources.
    sources: list[dict] = field(default_factory=lambda: [dict(s) for s in DEFAULT_SOURCES])
    watchlist: list[dict] = field(default_factory=list)

    # A posting passes only when category and title keyword both match and no exclude does;
    # docs/tuning.md has the cases each signal misses alone.
    allowed_categories: list[str] = field(
        default_factory=lambda: list(DEFAULT_ALLOWED_CATEGORIES))
    title_keywords: list[str] = field(
        default_factory=lambda: list(DEFAULT_TITLE_KEYWORDS))
    # Wrong seniority or type. wanted_intern_terms filters internships.
    title_exclude: list[str] = field(
        default_factory=lambda: list(DEFAULT_TITLE_EXCLUDE))
    # Keeps the broad "engineer" keyword from admitting hardware and field roles.
    title_exclude_field: list[str] = field(
        default_factory=lambda: list(DEFAULT_TITLE_EXCLUDE_FIELD))

    # An internship passes only when its feed `terms` include a wanted_intern_terms
    # entry; empty by default, so none pass.
    include_off_season_internships: bool = True
    intern_terms: list[str] = field(default_factory=lambda: list(DEFAULT_INTERN_TERMS))
    wanted_intern_terms: list[str] = field(default_factory=list)

    # A stated start year earlier than this flags the posting; an unstated one never
    # does. None disables it.
    graduation_year: int | None = None

    # The ranker weighs a posting's stated years against experience_years. A
    # full-time posting whose description asks for more than max_years_required is
    # left out; None keeps them all.
    experience_years: int = 0
    max_years_required: int | None = 2

    # A posting's `degrees` are the ones it accepts, so omitting yours excludes you.
    # Empty disables the check.
    degrees_held: list[str] = field(default_factory=list)

    # Keep only postings whose location matches one of these substrings, e.g.
    # ["CA", "New York", "Remote"]. Empty keeps every location.
    location_allow: list[str] = field(default_factory=list)
    us_only: bool = False            # skip postings whose every place is outside the US

    recent_days: int = 21            # only consider postings posted this recently
    fit_threshold: int = 60          # 0-100; below this no summary and no mention in the push
    # each costs one page fetch and one description_model call
    max_summaries_per_run: int = 25
    # stop ranking and summaries once the last 30 days hold this many model calls
    monthly_call_cap: int | None = None
    rank_batch_size: int = 25        # postings per ranking call; small enough to parse back
    rank_retries: int = 1            # extra attempts for a batch whose reply won't parse
    model_concurrency: int = 4       # ranking batches or summaries in flight at once
    # None scores every new posting; a cap takes the newest N, which starves older
    # postings from better companies.
    max_rank_per_run: int | None = None

    # The feeds carry no description text; distilling a fetched page is mechanical
    # work for the cheap model.
    fetch_descriptions: bool = True
    description_model: str = "haiku"

    # company names go to Clearbit and Wikidata, and website domains to DuckDuckGo and Google
    company_icons: bool = True

    # Below this company tier a posting stays listed but never gets a summary or a push.
    tier_floor: int = 55

    # Anyone who knows the topic can read the pushes, so it is a password. Empty
    # disables them.
    notify_ntfy_topic: str = ""
    notify_macos: bool = False       # local Mac banners; the phone push is the useful one
    # an applied posting with no later event for this many days needs a follow-up
    follow_up_days: int = 14
    notify_follow_ups: bool = True   # the push names applications that need one

    # USAJOBS sends its API key by email to the address it was requested with, and
    # requires both on every request. The key lives in secrets.toml under "usajobs";
    # a usajobs source is skipped while either is missing.
    usajobs_email: str = ""

    # Who answers ranking, summary and onboarding prompts. claude-code uses the
    # claude CLI; every other provider needs a key in secrets.toml. Empty models
    # pick the provider's defaults; the base URL only matters for ollama and custom.
    llm_provider: str = "claude-code"
    llm_model: str = ""
    llm_model_cheap: str = ""
    llm_base_url: str = ""

    claude_bin: str = "claude"       # or an absolute path, e.g. ~/.local/bin/claude
    claude_model: str = "sonnet"     # sonnet is plenty for ranking and far cheaper


# settings a past version read; config.toml files still carry them
RETIRED_KEYS = frozenset({"max_tailor_per_run", "max_fit_iters", "min_bullets",
                          "preset_model_matching", "reuse_resumes", "output_dir",
                          "usajobs_key"})


def defaults():
    return Settings()


def _matches(value, annotation):
    """Whether a value from TOML fits a field's declared shape."""
    if typing.get_origin(annotation) is types.UnionType:
        return any(_matches(value, arg) for arg in typing.get_args(annotation))
    if annotation is type(None):
        return value is None
    if typing.get_origin(annotation) is list:
        (item,) = typing.get_args(annotation)
        return isinstance(value, list) and all(_matches(v, item) for v in value)
    if annotation is int:  # bool is a subclass of int; true where 1 belongs is a mistake
        return isinstance(value, int) and not isinstance(value, bool)
    return isinstance(value, annotation)


def load(path=None):
    """Settings from a TOML file. A missing file means every default."""
    path = Path(path) if path else paths.config_file()
    try:
        with open(path, "rb") as f:
            raw = tomllib.load(f)
    except FileNotFoundError:
        return defaults()
    except tomllib.TOMLDecodeError as e:
        raise SettingsError(f"{path} is not valid TOML: {e}") from e
    retired = [key for key in raw if key in RETIRED_KEYS]
    if retired:
        events.emit("warn", text=f"{path.name}: ignoring retired setting(s): "
                                 f"{', '.join(retired)}")
    return from_dict({k: v for k, v in raw.items() if k not in RETIRED_KEYS}, source=path)


def _check_llm(raw, source):
    from cairn.ai import llm  # llm reads settings, so the import stays local
    provider = raw.get("llm_provider")
    if provider is not None and provider not in llm.PROVIDERS:
        raise SettingsError(f"{source}: 'llm_provider' should be one of "
                            f"{', '.join(llm.PROVIDERS)}, got '{provider}'")
    url = raw.get("llm_base_url")
    if url and not llm.valid_base_url(url):
        raise SettingsError(f"{source}: 'llm_base_url' should be an http(s) URL, got '{url}'")


def from_dict(raw, base=None, source="settings"):
    """base (default: every default) with the keys in raw replaced, each type-checked.

    `source` names where raw came from in the SettingsError message.
    """
    shapes = typing.get_type_hints(Settings)
    for key, value in raw.items():
        if key not in shapes:
            raise SettingsError(f"{source}: unknown setting '{key}'")
        if not _matches(value, shapes[key]):
            raise SettingsError(
                f"{source}: '{key}' should be {shapes[key]}, got {type(value).__name__}")
        _check_range(key, value, source)
    _check_llm(raw, source)
    for key, kinds in _SPEC_KINDS.items():
        for i, spec in enumerate(raw.get(key, ())):
            _check_spec(spec, kinds, f"{source}: {key}[{i}]")
    return dataclasses.replace(base or defaults(), **raw)


# inclusive bounds of the numeric settings; None leaves the top open
_RANGES = {
    "follow_up_days": (1, 90),
    "monthly_call_cap": (1, None),
    "fit_threshold": (0, 100),
    "tier_floor": (0, 100),
    "recent_days": (1, 365),
    "experience_years": (0, 50),
    "max_years_required": (0, 50),
    "rank_batch_size": (1, 100),
    "rank_retries": (0, 5),
    "model_concurrency": (1, 8),
    "max_summaries_per_run": (0, 500),
    "max_rank_per_run": (1, None),
}


def _check_range(key, value, source):
    if key not in _RANGES or value is None:
        return
    low, high = _RANGES[key]
    if value < low or (high is not None and value > high):
        span = f"at least {low}" if high is None else f"between {low} and {high}"
        raise SettingsError(f"{source}: '{key}' should be {span}, got {value}")


_SPEC_KINDS = {"sources": sources.KINDS, "watchlist": sources.BOARD_KINDS}
_SPEC_FIELDS = {"kind": str, "location": str, "company": str, "enabled": bool}


def _check_spec(spec, kinds, where):
    for key, value in spec.items():
        if key not in _SPEC_FIELDS:
            raise SettingsError(f"{where}: unknown key '{key}'")
        if not isinstance(value, _SPEC_FIELDS[key]):
            raise SettingsError(f"{where}: '{key}' should be {_SPEC_FIELDS[key].__name__}, "
                                f"got {type(value).__name__}")
    if spec.get("kind") not in kinds:
        raise SettingsError(f"{where}: kind should be one of {', '.join(kinds)}, "
                            f"got {spec.get('kind')!r}")
    if not spec.get("location"):
        raise SettingsError(f"{where}: missing location")
    fits, wanted = _LOCATIONS.get(spec["kind"], (None, None))
    if fits is not None and not fits(spec["location"]):
        raise SettingsError(f"{where}: location should be {wanted}")
    if spec["kind"] == "page" and not spec.get("company"):
        raise SettingsError(f"{where}: a page source needs a company")


def _path(url):
    return urlparse(url).path.strip("/").split("/")


def _is_web(url):
    parsed = urlparse(url)
    return parsed.scheme in ("http", "https") and bool(parsed.hostname)


def _on_host(url, *hosts):
    return _is_web(url) and urlparse(url).hostname in hosts


# kind: (whether a location suits it, what it should be)
_LOCATIONS = {
    "github": (lambda location: _on_host(location, "raw.githubusercontent.com")
              and len(_path(location)) >= 2,
              "a raw.githubusercontent.com listings.json URL"),
    "workday": (lambda location: sources.workday_board(location) is not None,
                "<tenant>.<wdN>/<site>, as in https://<tenant>.<wdN>.myworkdayjobs.com/<site>"),
    # owner, repo, branch, then the file
    "github_readme": (lambda location: _on_host(location, "raw.githubusercontent.com")
                      and len(_path(location)) >= 4 and location.lower().endswith(".md"),
                      "a raw.githubusercontent.com URL of a Markdown file"),
    "hn_hiring": (lambda location: location == sources.HN_LATEST
                  or sources.hn_thread(location) is not None,
                  f'"{sources.HN_LATEST}" or a news.ycombinator.com/item?id= thread URL'),
    "yc_waas": (lambda location: _on_host(location, "www.ycombinator.com")
                and urlparse(location).path.startswith("/jobs"),
                "a www.ycombinator.com/jobs URL"),
    "remoteok": (lambda location: _on_host(location, "remoteok.com", "www.remoteok.com"),
                 sources.REMOTEOK_API),
    "page": (_is_web, "an http(s) URL"),
}


HEADER = "# Cairn settings. Anything left out keeps its default."


def _toml_string(text):
    # json.dumps with ensure_ascii off yields a TOML basic string, except that JSON
    # leaves DEL raw where TOML requires an escape. With ensure_ascii on, JSON would
    # escape a non-BMP character as a surrogate pair, which TOML rejects.
    return json.dumps(text, ensure_ascii=False).replace("\x7f", "\\u007f")


def _toml(value):
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        return _toml_string(value)
    return "[" + ", ".join(_toml_string(item) for item in value) + "]"


def _is_table_array(value):
    return isinstance(value, list) and bool(value) and all(isinstance(v, dict) for v in value)


def _toml_tables(name, tables):
    lines = []
    for table in tables:
        lines += ["", f"[[{name}]]"] + [f"{k} = {_toml(v)}" for k, v in table.items()]
    return lines


def save(settings, path=None):
    """Write the settings that differ from the defaults. Returns the path.

    Rewrites the file whole, dropping the comments of a hand-edited or example file.
    """
    path = Path(path) if path else paths.config_file()
    base = defaults()
    lines, tables = [HEADER, ""], []
    for f in dataclasses.fields(settings):
        value = getattr(settings, f.name)
        if value == getattr(base, f.name):
            continue
        # TOML puts every key after the first table header inside that table
        if _is_table_array(value):
            tables += _toml_tables(f.name, value)
        else:
            lines.append(f"{f.name} = {_toml(value)}")
    lines += tables
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


_current = None

# A ContextVar set in one thread is invisible to every other thread, so a run's
# overrides stay inside its own thread and never reach the server's request threads.
_override = contextvars.ContextVar("settings_override", default=None)


def base():
    """The process-wide settings, loaded from disk on first use, ignoring any override."""
    global _current
    if _current is None:
        _current = load()
    return _current


def get():
    """The settings this context runs on: its override if one is active, else base()."""
    current = _override.get()
    return base() if current is None else current


@contextmanager
def override(cfg):
    """Make get() return cfg in this context until the block exits."""
    token = _override.set(cfg)
    try:
        yield
    finally:
        _override.reset(token)


def use(settings):
    global _current
    _current = settings


def reset():
    global _current
    _current = None
