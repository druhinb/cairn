"""cairn init: example files, or a profile drafted from a resume and a few questions."""
import sys
from importlib import resources
from pathlib import Path

from cairn.ai import onboard
from cairn.core import paths, ui


def _example_files():
    """(packaged example, destination under the home directory) for every seed file."""
    examples = resources.files("cairn") / "examples"
    home = paths.home()
    for item in sorted(examples.iterdir(), key=lambda p: p.name):
        if item.is_file():
            yield item, home / item.name


def _seed_examples():
    created, kept = [], []
    for src, dest in _example_files():
        if dest.exists():
            kept.append(dest)
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(src.read_bytes())
        created.append(dest)
    for dest in created:
        ui.info(f"  wrote {dest}")
    for dest in kept:
        ui.info(f"  kept {dest} (already there)")
    ui.summary([("home", paths.home()),
                ("written", len(created)),
                ("left alone", len(kept))], title="init")


def _split(text, separator):
    return [part.strip() for part in text.split(separator) if part.strip()]


# key, answer kind, question; each answer is checked by onboard before it is kept
PREF_QUESTIONS = [
    ("roles", "list", f"Roles ({', '.join(onboard.ROLE_KEYWORDS)})"),
    ("locations", "list", "Locations to work in, empty for anywhere"),
    ("remote_ok", "bool", "Remote OK"),
    ("work_authorization", "text",
     f"Work authorization ({' / '.join(onboard.WORK_AUTHORIZATION)})"),
    ("graduation_year", "int", "Graduation year"),
    ("degrees_held", "list", "Degrees held (Bachelor's, Master's, PhD, Associate's)"),
    ("internship_terms", "list", "Internship terms you can take, e.g. fall 2026"),
    ("calibre_anchors", "anchors",
     "Up to 3 company tier examples separated by ';', e.g. Stripe, Databricks = 90"),
    ("ntfy_topic", "text", "ntfy topic for phone pushes (treat it as a password)"),
    ("watchlist", "list", "Companies to watch (names or job board URLs)"),
]
_PARSE = {
    "list": lambda text: _split(text, ","),
    "anchors": lambda text: _split(text, ";"),
    "bool": lambda text: text.lower() in ("y", "yes", "true"),
    "int": lambda text: None if text.lower() == "none" else int(text),
    "text": lambda text: text,
}


def _shown(value, kind):
    if kind == "bool":
        return "y" if value else "n"
    if value is None:
        return "none"
    if isinstance(value, list):
        return ("; " if kind == "anchors" else ", ").join(value)
    return str(value)


def _ask(answers, questions, check=None):
    """answers with each question asked on the terminal; an empty reply keeps the default.

    check(key, value) raises ValueError or OnboardError to ask again.
    """
    answers = dict(answers)
    for key, kind, question in questions:
        while True:
            reply = input(f"{question} [{_shown(answers[key], kind)}]: ").strip()
            if not reply:
                break
            try:
                value = _PARSE[kind](reply)
                if check:
                    check(key, value)
            except (ValueError, onboard.OnboardError) as e:
                ui.error(str(e))
                continue
            answers[key] = value
            break
    return answers


def _ask_prefs(prefs):
    ui.rule("Preferences")
    return _ask(prefs, PREF_QUESTIONS,
                check=lambda key, value: onboard.checked_prefs({key: value}))


def _apply_draft(draft, prefs, overwrite):
    try:
        applied = onboard.apply(draft, {**prefs, "overwrite": overwrite})
    except onboard.OnboardError as e:
        ui.error(str(e))
        return 1
    for path in applied.written:
        ui.info(f"  wrote {path}")
    for name in applied.unresolved:
        ui.warn(f"  no job board found for {name}. Add it under [[watchlist]] in config.toml by hand")
    ui.info("Then run: cairn doctor, and cairn seed")
    return 0


def _init_from_resume(resume, accept_defaults, overwrite):
    # refused before the Claude call, which apply would refuse anyway
    own = [] if overwrite else onboard.conflicts()
    if own:
        ui.error(str(onboard.FilesExist(own)))
        return 1
    try:
        text = onboard.resume_text(Path(resume).expanduser())
        ui.info("Cairn is reading your resume, 20-60 s")
        draft = onboard.draft_from_resume(text)
    except onboard.OnboardError as e:
        ui.error(str(e))
        return 1
    directory = onboard.save_draft(draft)
    ui.info(f"  wrote the drafts to {directory}")
    if not (accept_defaults or sys.stdin.isatty()):
        ui.info(f"Review the drafts in {directory}, then apply them with: "
                "cairn init --apply-draft")
        return 0
    prefs = onboard.default_prefs(draft.suggestions)
    if not accept_defaults:
        prefs = _ask_prefs(prefs)
    onboard.save_prefs(prefs)
    return _apply_draft(draft, prefs, overwrite)


def _init_from_saved_draft(overwrite):
    try:
        draft, prefs = onboard.load_draft()
    except onboard.OnboardError as e:
        ui.error(str(e))
        return 1
    return _apply_draft(draft, prefs, overwrite)


def cmd_init(args):
    """Seed the home directory with the example config.toml and profile.md.

    An existing file is always left alone. Once edited these files are the user's own
    content, and a second `init` must never be able to erase them. With --from-resume
    or --apply-draft, profile.md is then replaced by a draft from a resume, but only
    while it still holds the example, unless --overwrite is given.
    """
    if args.overwrite and not (args.from_resume or args.apply_draft):
        ui.error("--overwrite only applies with --from-resume or --apply-draft")
        return 2
    _seed_examples()
    if args.from_resume:
        return _init_from_resume(args.from_resume, args.yes, args.overwrite)
    if args.apply_draft:
        return _init_from_saved_draft(args.overwrite)
    ui.info("Edit profile.md, then run: cairn seed")
    return 0
