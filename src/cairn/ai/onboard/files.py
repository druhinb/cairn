"""The files setup writes, and the draft saved between init steps."""
import dataclasses
import json
import os
import tempfile
from dataclasses import dataclass

from cairn import sources, store
from cairn.ai.onboard.preferences import (
    _mapped,
    checked_prefs,
    default_prefs,
    with_answers,
)
from cairn.ai.onboard.resume import Draft, OnboardError, _example, _suggestions
from cairn.core import paths, settings
from cairn.sources import watchlists


class FilesExist(OnboardError):
    def __init__(self, existing):
        names = ", ".join(str(p) for p in existing)
        super().__init__(f"these files hold your own content: {names}. Set overwrite, "
                         "or pass --overwrite to cairn init, to replace them.")
        self.paths = existing


@dataclass
class Applied:
    written: list
    unresolved: list
    settings: settings.Settings


def _write_atomic(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent,
                                     prefix=f".{path.name}.", delete=False) as f:
        f.write(text)
    os.replace(f.name, path)


def _is_example(path):
    return path.read_text(encoding="utf-8", errors="replace") == _example(path.name)


def _holds_own_content(path):
    return path.exists() and not _is_example(path)


def conflicts():
    """The files apply refuses to replace without overwrite: profile.md once it holds
    the user's own content."""
    path = paths.profile_md()
    return [path] if _holds_own_content(path) else []


def initialised():
    return paths.profile_md().exists()


def needs_setup():
    """True while profile.md is missing or still the packaged example."""
    path = paths.profile_md()
    return not path.exists() or _is_example(path)


def _watchlist(entries, existing):
    """(existing plus each entry's board, the entries no board was found for)."""
    boards, unresolved = [dict(spec) for spec in existing], []
    for entry in entries:
        found = sources.resolve_company(entry)
        if found is None:
            unresolved.append(entry)
            continue
        if not any((b["kind"], b["location"]) == (found.kind, found.location) for b in boards):
            boards.append({"kind": found.kind, "location": found.location,
                           "company": found.company})
    return boards, unresolved


def apply(draft, prefs):
    """Write profile.md and config.toml from a draft and the answers.

    The packaged example profile.md that `init` copied is replaced freely; one
    holding the user's own content is replaced only when prefs["overwrite"] is true.
    """
    prefs = checked_prefs(prefs)
    profile = with_answers(draft.profile_md, prefs)
    if not profile.strip():
        raise OnboardError("profile.md is empty")
    if not prefs.get("overwrite"):
        own = conflicts()
        if own:
            raise FilesExist(own)
    new = _mapped(prefs, settings.base())
    boards, unresolved = _watchlist(prefs.get("watchlist", []), new.watchlist)
    for starter_id in prefs.get("starter_watchlists", []):
        boards = watchlists.merge(boards, watchlists.starter(starter_id)[2])[0]
    new = dataclasses.replace(new, watchlist=boards)
    _write_atomic(paths.profile_md(), profile)
    written = [paths.profile_md(), settings.save(new)]
    settings.use(new)
    store.sync_relevance()
    return Applied(written, unresolved, new)


def answer_again(prefs):
    """apply with the current profile.md as the draft, for answers given again
    without a resume. The answers replace what earlier ones wrote into it, and the
    packaged example is refused, as it describes someone else."""
    if needs_setup():
        raise OnboardError("Cairn has no profile of yours yet. Run setup first")
    text = paths.profile_md().read_text(encoding="utf-8")
    return apply(Draft(text, {}), {**prefs, "overwrite": True})


DRAFT_FILES = ("profile.md", "suggestions.json")
PREFS_FILE = "prefs.json"


def draft_dir():
    return paths.home() / "onboard-draft"


def save_draft(draft):
    """Write the draft's files. Returns their directory."""
    directory = draft_dir()
    texts = (draft.profile_md, json.dumps(draft.suggestions, indent=2) + "\n")
    for name, text in zip(DRAFT_FILES, texts):
        _write_atomic(directory / name, text)
    return directory


def save_prefs(prefs):
    _write_atomic(draft_dir() / PREFS_FILE, json.dumps(prefs, indent=2) + "\n")


def load_draft():
    """(Draft, prefs) from draft_dir(); prefs are the saved answers, else the defaults."""
    directory = draft_dir()
    missing = [name for name in DRAFT_FILES if not (directory / name).is_file()]
    if missing:
        raise OnboardError(f"no saved draft in {directory} (missing {', '.join(missing)}). "
                           "Run: cairn init --from-resume FILE")
    profile, raw = ((directory / name).read_text(encoding="utf-8") for name in DRAFT_FILES)
    draft = Draft(profile, _suggestions(raw))
    saved = directory / PREFS_FILE
    if not saved.exists():
        return draft, default_prefs(draft.suggestions)
    try:
        return draft, json.loads(saved.read_text(encoding="utf-8"))
    except ValueError as e:
        raise OnboardError(f"{saved} is not valid JSON: {e}") from None
