"""Daily new-grad job feed.

Fetches postings, ranks them for fit and company tier, and summarises the
requirements of the top matches. A run marks postings seen only after it stores
their scores, so a failed run retries next time.
"""
import argparse
import getpass
import io
import sys
import time
from importlib import resources
from pathlib import Path

try:  # the first project import, so a missing rich fails with the hint below
    from cairn.core import ui
except ImportError:
    print("Cairn cannot import rich; reinstall the package: "
          "uv pip install -e <your cairn checkout>", file=sys.stderr)
    sys.exit(2)

from cairn import applications, store
from cairn.ai import claude, llm, onboard
from cairn.core import logfile, paths, secrets, settings
from cairn.jobs import fetch, logos, notify, pipeline
from cairn.system import backup, doctor, schedule, update

TOP_SHOWN = 15  # ranked postings echoed to the terminal; the app has them all
LOGGED_COMMANDS = {"run", "serve", "icons"}  # their events are appended to paths.run_log()
JOB_FILE = "entry" if sys.platform == "win32" else "plist"


def cmd_run(args):
    opts = pipeline.RunOptions(limit=args.limit, fit=args.fit, dry_run=args.dry_run)
    try:
        result = pipeline.run(opts)
    except pipeline.RunInProgress as e:
        ui.error(str(e))
        return 3
    if result.status == "dry-run":
        ui.postings(result.results)
        ui.info("dry run: nothing ranked, summarized, or marked seen.")
        return 0

    results, counts = result.results, result.counts
    # the fit colours and the notification's bar follow this run's --fit
    with pipeline.run_settings(opts):
        # Seeing the best few scored right here is what tells you whether the run
        # was worth opening the app for.
        ui.postings(results[:TOP_SHOWN], extra=max(0, len(results) - TOP_SHOWN))
        ui.summary([
            ("fetched", counts["total"]),
            ("relevant/recent", counts["relevant"]),
            ("new", counts["new"]),
            ("ranked", counts["ranked"]),
            ("unranked, left unseen", counts["unranked"]),
            ("summarised", counts["summarised"]),
            ("repeat companies", sum(1 for r in results if r.get("repeat"))),
            ("elapsed", f"{result.elapsed:.1f}s"),
        ], title="run complete")
        notify.run_finished(results, follow_ups=result.follow_ups)
    return 0


def cmd_fetch(args):
    new, _ = fetch.fetch_new()
    ui.postings(new[:args.limit], extra=max(0, len(new) - args.limit))
    ui.info("fetch is a dry run and marked nothing seen.")
    return 0


def cmd_seed(args):
    fetch.seed()
    return 0


def _plural(count, noun):
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


def cmd_icons(args):
    if not settings.get().company_icons:
        ui.error("company icons are off. Set company_icons = true in config.toml")
        return 1
    try:
        with logos.icon_job():
            if args.reset_failures:
                companies, icons = store.clear_icon_failures()
                ui.info(f"cleared {_plural(companies, 'failed website lookup')} and "
                        f"{_plural(icons, 'failed icon download')}.")
            job = logos.fetch_all(limit=args.limit)
    except logos.IconsRunning as e:
        ui.error(str(e))
        return 3
    counts = store.icon_counts()
    ui.info(f"fetched {_plural(job.fetched, 'icon')}. {counts['with_icon']} of "
            f"{counts['companies']} companies with active postings now have one.")
    return 1 if job.offline else 0


def _last_run():
    runs = store.list_runs(1)
    if not runs:
        return "none yet"
    return f"{runs[0]['status']}, started {runs[0]['started_at']}"


# label on the `status` pipeline line -> the statuses it adds up
PIPELINE_LINE = {"saved": ("saved",), "applied": ("applied",),
                 "interviewing": ("interviewing",), "offer": ("offer",),
                 "closed": ("rejected", "withdrawn")}


def _pipeline_line(by_status):
    return " · ".join(f"{label} {sum(by_status[s] for s in statuses)}"
                      for label, statuses in PIPELINE_LINE.items())


def cmd_status(args):
    total, this_week, recent = applications.stats()
    counts = store.counts()
    ui.summary([
        ("home", paths.home()),
        ("postings", f"{counts['postings']} total, {counts['active']} active"),
        ("postings seen", counts["seen"]),
        ("postings scored", counts["scored"]),
        ("applications", f"{total} sent, {this_week} in the last 7 days"),
        ("pipeline", _pipeline_line(counts["applications"])),
        ("runs", f"{counts['runs']} (last: {_last_run()})"),
    ], title="Cairn")
    ui.recent_applications(recent)
    return 0


def _unknown_posting(job_id):
    ui.error(f"unknown posting id: {job_id}. The run output prints an id beside "
             "every posting, and any unambiguous prefix works.")
    return 1


def cmd_applied(args):
    entry = applications.record(args.job_id, args.note)
    if entry is None:
        return _unknown_posting(args.job_id)
    total = store.counts()["applied"]
    ui.info(f"logged {entry['company']} — {entry['title']} ({total} sent).")
    return 0


def cmd_track(args):
    if args.clear and (args.status is not None or args.note is not None):
        ui.error("--clear takes no status or note")
        return 2
    if not args.clear and args.status is None and args.note is None:
        ui.error("give a status, a --note, or --clear")
        return 2
    job_id = store.resolve(args.job_id)
    if job_id is None:
        return _unknown_posting(args.job_id)
    if args.clear:
        store.clear_application(job_id)
        ui.info(f"cleared the status of {job_id}.")
        return 0
    if args.status is None:
        entry = store.set_note(job_id, args.note)
        if entry is None:
            ui.error(f"{job_id} has no status yet. Give one along with the note.")
            return 2
    else:
        entry = store.set_status(job_id, args.status, args.note)
    ui.info(f"{entry['company']} — {entry['title']}: {entry['status']}.")
    return 0


LOCAL_HOSTS = ("127.0.0.1", "localhost", "::1")


def cmd_serve(args):
    from cairn import server  # noqa: PLC0415 - keeps fastapi out of every other command
    try:
        server.serve(args.host, args.port)
    except (OSError, RuntimeError) as e:
        ui.error(f"could not serve on {args.host}:{args.port or 'a free port'}: {e}")
        return 1
    return 0


def cmd_ui(args):
    from cairn import server  # noqa: PLC0415 - keeps fastapi out of every other command
    from cairn.desktop import window  # noqa: PLC0415
    port = args.port or server.pick_port(window.HOST)
    try:
        window.open_ui(args.no_window, port)
    except (OSError, RuntimeError) as e:
        ui.error(f"could not open the app on port {port}: {e}")
        return 1
    return 0


def cmd_doctor(args):
    results = doctor.checks()
    for check in results:
        mark = "✓" if check["ok"] else "✗"
        optional = "" if check["required"] else " (optional)"
        ui.info(f"{mark} {check['name']}{optional}: {check['detail']}")
        for line in check["fix"].splitlines():
            ui.info(f"    fix: {line}")
    return 1 if any(c["required"] and not c["ok"] for c in results) else 0


def cmd_schedule(args):
    actions = {"install": lambda: schedule.install(args.hour, args.minute),
               "remove": schedule.remove, "status": schedule.status}
    try:
        job = actions[args.action]()
    except schedule.ScheduleError as e:
        ui.error(str(e))
        return 1
    when = "-" if job["hour"] is None else f"{job['hour']}:{job['minute'] or 0:02d}"
    rows = [("installed", "yes" if job["installed"] else "no"),
            ("loaded", "yes" if job["loaded"] else "no"),
            ("daily at", when),
            ("runs", job["program"] or "-"),
            (JOB_FILE, job["plist"])]
    if job["error"]:
        rows.append(("error", job["error"]))
    ui.summary(rows, title=f"schedule {args.action}")
    return 0


def cmd_autostart(args):
    actions = {"install": schedule.autostart_install, "remove": schedule.autostart_remove,
               "status": schedule.autostart_status}
    try:
        item = actions[args.action]()
    except schedule.ScheduleError as e:
        ui.error(str(e))
        return 1
    rows = [("installed", "yes" if item["installed"] else "no"),
            ("loaded", "yes" if item["loaded"] else "no"),
            ("opens", item["program"] or "-"),
            (JOB_FILE, item["plist"])]
    if item["error"]:
        rows.append(("error", item["error"]))
    ui.summary(rows, title=f"autostart {args.action}")
    if args.action == "install" and not item["loaded"]:
        ui.info("The app opens at your next login.")
    return 0


def cmd_update_check(args):
    found = update.check(force=args.force)
    if found is None:
        return 1  # check() has already warned why
    if found["newer"]:
        where = f": {found['url']}" if found["url"] else ""
        ui.info(f"Cairn {found['latest']} is out (you have {found['current']}){where}")
    else:
        ui.info(f"Cairn {found['current']} is the latest release")
    return 0


def cmd_backup(args):
    try:
        path = backup.export(args.to)
    except OSError as e:
        ui.error(f"backup failed: {e}")
        return 1
    ui.info(f"wrote {path} ({path.stat().st_size:,} bytes)")
    return 0


def _confirmed(question):
    return input(f"{question} [y/N]: ").strip().lower() in ("y", "yes")


def cmd_restore(args):
    if backup.app_running():
        ui.error("the app is open and would keep using the old files, so quit it first")
        return 3
    if not args.yes:
        if not sys.stdin.isatty():
            ui.error("restore replaces your data. Pass --yes to confirm")
            return 2
        if not _confirmed(f"Replace the config, profile, database, and icons in "
                          f"{paths.home()} with the backup's?"):
            ui.info("restore canceled. Nothing changed.")
            return 1
    try:
        aside = backup.restore(Path(args.zip).expanduser(), accept_model_change=True)
    except backup.BackupError as e:
        ui.error(str(e))
        return 1
    except pipeline.RunInProgress as e:
        ui.error(str(e))
        return 3
    ui.info(f"restored {args.zip}. The previous files are in {aside}")
    return 0


def cmd_settings_export(args):
    try:
        sys.stdout.write(backup.export_settings())
    except FileNotFoundError:
        ui.error(f"no config.toml in {paths.home()}. Create one with: cairn init")
        return 1
    return 0


def cmd_llm(args):
    if args.action == "set":
        return _llm_set(args)
    if args.action == "test":
        return _llm_test()
    provider = llm.active()
    ui.info(f"  provider  {provider} ({llm.PROVIDERS[provider]['label']})")
    ui.info(f"    strong  {llm.target('strong').model or '-'}")
    ui.info(f"     cheap  {llm.target('cheap').model or '-'}")
    ui.info(f"  base url  {llm.target('strong').base_url or '-'}")
    ui.info(f"       key  {'present' if secrets.has_key(provider) else 'none'}")
    return 0


def _llm_set_refusal(args):
    """Why `llm set` cannot take these arguments, or None; the rules PUT /api/llm keeps."""
    if args.provider not in llm.PROVIDERS:
        return f"unknown provider {args.provider!r}, expected one of: {', '.join(llm.PROVIDERS)}"
    if args.base_url and args.provider not in llm.EDITABLE_BASE_URL:
        return "--base-url: only ollama and custom take one"
    if args.base_url and not llm.valid_base_url(args.base_url):
        return f"--base-url: should be an http(s) URL, got {args.base_url!r}"
    if args.provider == "custom" and not args.base_url:
        return "--base-url: the custom provider needs one"
    return None


def _llm_set(args):
    refusal = _llm_set_refusal(args)
    if refusal:
        ui.error(refusal)
        return 2
    if args.key_stdin:
        key = sys.stdin.readline().strip()
    elif llm.PROVIDERS[args.provider]["needs_key"] and sys.stdin.isatty():
        key = getpass.getpass("API key (leave empty to keep): ").strip()
    else:
        key = ""
    cfg = settings.from_dict({"llm_provider": args.provider, "llm_model": args.model or "",
                              "llm_model_cheap": args.cheap_model or "",
                              "llm_base_url": args.base_url or ""},
                             base=settings.base(), source="command line")
    if key:
        secrets.set_key(args.provider, key)
    settings.save(cfg)
    settings.use(cfg)
    return cmd_llm(argparse.Namespace(action=None))


def _llm_test():
    target = llm.target()
    started = time.monotonic()
    if llm.PROVIDERS[target.provider]["kind"] == "cli":
        reply, error = claude.run_cli(llm.TEST_PROMPT, None, 20)
    else:
        reply, error = llm.send(target, llm.TEST_PROMPT, 20, retries=0)
    ms = round((time.monotonic() - started) * 1000)
    if error:
        ui.error(llm.redact(error, target.key))
        return 1
    ui.info(f"ok {ms} ms: {llm.redact((reply or '').strip()[:200], target.key)}")
    return 0


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
    """prefs with each preference answered on the terminal."""
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
    while it still holds the example.
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


def _positive(text):
    """argparse type: reject N < 1, which would slice the list from the wrong end."""
    n = int(text)
    if n < 1:
        raise argparse.ArgumentTypeError(f"must be 1 or more, got {n}")
    return n


def _in_range(name, low, high):
    """argparse type: an int from low to high inclusive."""
    def parse(text):
        n = int(text)
        if not low <= n <= high:
            raise argparse.ArgumentTypeError(f"must be {low}-{high}, got {n}")
        return n
    parse.__name__ = name  # argparse reports a non-int as "invalid <name> value"
    return parse


def _parser():
    kwargs = {}
    if sys.version_info >= (3, 14):
        # Python 3.14's argparse colourises its own usage and error output, which
        # ui.py cannot intercept. Left on, a bad argument under launchd writes raw
        # escapes into launchd.err.
        kwargs["color"] = ui.IS_TTY
    parser = argparse.ArgumentParser(
        prog="cairn", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter, **kwargs)
    sub = parser.add_subparsers(
        dest="cmd",
        metavar="{init,run,fetch,seed,icons,status,applied,track,serve,ui,doctor,schedule,"
                "autostart,update-check,backup,restore,settings,llm}")

    init_p = sub.add_parser("init", help=f"seed {paths.home()} with example files")
    drafts = init_p.add_mutually_exclusive_group()
    drafts.add_argument("--from-resume", metavar="FILE",
                        help="draft profile.md from a .pdf, .txt, or .md resume")
    drafts.add_argument("--apply-draft", action="store_true",
                        help="apply the draft a previous --from-resume saved")
    init_p.add_argument("--yes", action="store_true",
                        help="with --from-resume, accept the suggested answers without asking")
    init_p.add_argument("--overwrite", action="store_true",
                        help="with --from-resume or --apply-draft, replace profile.md "
                             "even when edited")
    init_p.set_defaults(func=cmd_init)

    run = sub.add_parser("run", help="fetch, rank, and summarize new postings")
    run.add_argument("--limit", type=_positive, metavar="N",
                     help="process only the N newest postings and leave the rest unseen")
    run.add_argument("--dry-run", action="store_true",
                     help="list the postings a run would process, without ranking them or marking them seen")
    run.add_argument("--fit", type=int, metavar="N",
                     help="fit score needed for a summary and the push (default "
                          f"{settings.defaults().fit_threshold}). Applies to this "
                          "run only and leaves config.toml unchanged")
    run.set_defaults(func=cmd_run)

    fetch_p = sub.add_parser("fetch", help="dry run: show new postings, rank nothing")
    fetch_p.add_argument("--limit", type=_positive, default=25, metavar="N",
                         help="how many postings to list (default 25)")
    fetch_p.set_defaults(func=cmd_fetch)

    seed_p = sub.add_parser("seed", help="mark the current backlog as seen (run once)")
    seed_p.set_defaults(func=cmd_seed)

    icons_p = sub.add_parser("icons", help="look up company websites and download their icons")
    icons_p.add_argument("--limit", type=_positive, metavar="N",
                         help="look up at most N companies (default: every one)")
    icons_p.add_argument("--reset-failures", action="store_true",
                         help="retry the companies whose icon lookup failed before")
    icons_p.set_defaults(func=cmd_icons)

    status_p = sub.add_parser("status", help="postings stored, applications logged, last run")
    status_p.set_defaults(func=cmd_status)

    applied_p = sub.add_parser("applied", help="log that you applied to a posting")
    applied_p.add_argument("job_id", metavar="ID", help="posting id, as run prints it")
    applied_p.add_argument("--note", default="", help="a note to keep with the posting")
    applied_p.set_defaults(func=cmd_applied)

    track_p = sub.add_parser("track", help="set where a posting stands, or --clear it")
    track_p.add_argument("job_id", metavar="ID", help="posting id, as run prints it")
    track_p.add_argument("status", nargs="?", choices=store.STATUSES,
                         help="saved, applied, interviewing, offer, rejected, withdrawn, "
                              "or passed (not interested)")
    track_p.add_argument("--note", help="replaces the stored note")
    track_p.add_argument("--clear", action="store_true",
                         help="forget the posting's status, note, and history")
    track_p.set_defaults(func=cmd_track)

    serve_p = sub.add_parser("serve", help="run the local web app")
    serve_p.add_argument("--host", default="127.0.0.1", choices=LOCAL_HOSTS,
                         help="loopback address to listen on (default 127.0.0.1)")
    serve_p.add_argument("--port", type=_positive, metavar="N",
                         help="port to listen on (default: any free port)")
    serve_p.set_defaults(func=cmd_serve)

    ui_p = sub.add_parser("ui", help="open the app in a window")
    ui_p.add_argument("--no-window", action="store_true",
                      help="open it in the browser instead, and serve until Ctrl-C")
    ui_p.add_argument("--port", type=_positive, metavar="N",
                      help="port to serve on (default: any free port)")
    ui_p.set_defaults(func=cmd_ui)

    doctor_p = sub.add_parser("doctor", help="check that this machine can run the pipeline")
    doctor_p.set_defaults(func=cmd_doctor)

    schedule_p = sub.add_parser("schedule", help="install, remove, or show the daily launchd run")
    actions = schedule_p.add_subparsers(dest="action", required=True,
                                        metavar="{install,remove,status}")
    install_p = actions.add_parser("install", help="write and load the launchd job")
    install_p.add_argument("--hour", type=_in_range("hour", 0, 23),
                           default=schedule.DEFAULT_HOUR, metavar="H",
                           help=f"hour of the daily run, 0-23 (default {schedule.DEFAULT_HOUR})")
    install_p.add_argument("--minute", type=_in_range("minute", 0, 59),
                           default=schedule.DEFAULT_MINUTE, metavar="M",
                           help=f"minute of the daily run (default {schedule.DEFAULT_MINUTE})")
    actions.add_parser("remove", help="unload and delete the launchd job")
    actions.add_parser("status", help="show the installed launchd job")
    schedule_p.set_defaults(func=cmd_schedule)

    autostart_p = sub.add_parser("autostart", help="open the app at login, or stop that")
    autostart_actions = autostart_p.add_subparsers(dest="action", required=True,
                                                   metavar="{install,remove,status}")
    autostart_actions.add_parser("install", help="open the app at every login")
    autostart_actions.add_parser("remove", help="stop opening the app at login")
    autostart_actions.add_parser("status", help="show whether the app opens at login")
    autostart_p.set_defaults(func=cmd_autostart)

    update_p = sub.add_parser("update-check", help="check GitHub for a newer release")
    update_p.add_argument("--force", action="store_true",
                          help="ask GitHub even when it was asked in the last 24 hours")
    update_p.set_defaults(func=cmd_update_check)

    backup_p = sub.add_parser("backup", help="save config, profile, database, and icons "
                                             "to one zip")
    backup_p.add_argument("--to", metavar="DIR", help="folder for the zip (default ~/Downloads)")
    backup_p.set_defaults(func=cmd_backup)

    restore_p = sub.add_parser("restore", help="replace your data with a backup's")
    restore_p.add_argument("zip", metavar="ZIP", help="a zip that `cairn backup` wrote")
    restore_p.add_argument("--yes", action="store_true", help="restore without asking")
    restore_p.set_defaults(func=cmd_restore)

    settings_p = sub.add_parser("settings", help="print config.toml")
    settings_actions = settings_p.add_subparsers(dest="action", required=True,
                                                 metavar="{export}")
    settings_actions.add_parser("export", help="print config.toml to standard output")
    settings_p.set_defaults(func=cmd_settings_export)

    llm_p = sub.add_parser("llm", help="show, set, or test the model provider")
    llm_actions = llm_p.add_subparsers(dest="action", metavar="{set,test}")
    set_p = llm_actions.add_parser("set", help="choose a provider and store its key")
    set_p.add_argument("provider", metavar="PROVIDER", help=", ".join(llm.PROVIDERS))
    set_p.add_argument("--model", help="the model for ranking (empty uses the provider's default)")
    set_p.add_argument("--cheap-model", help="the model for summaries")
    set_p.add_argument("--base-url", help="the endpoint, for ollama and custom")
    set_p.add_argument("--key-stdin", action="store_true", help="read the API key from stdin")
    llm_actions.add_parser("test", help="send one short prompt and time the answer")
    llm_p.set_defaults(func=cmd_llm)
    return parser


BACKGROUND_OUTPUT = {"stdout": "background.out", "stderr": "background.err"}


def _windows_output():
    """UTF-8 output on Windows. pythonw, which the daily task and the login entry
    start, has no console, so its output goes to files in the data home, as launchd's
    does on a Mac."""
    for name, file in BACKGROUND_OUTPUT.items():
        stream = getattr(sys, name)
        if stream is None:
            paths.home().mkdir(parents=True, exist_ok=True)
            setattr(sys, name, open(paths.home() / file, "a", encoding="utf-8"))
        elif isinstance(stream, io.TextIOWrapper):
            # a redirected stream writes the ANSI code page, which lacks ✓ and most
            # company names outside Western Europe
            stream.reconfigure(encoding="utf-8", errors="replace")


def main():
    if sys.platform == "win32":
        _windows_output()
    parser = _parser()
    args = parser.parse_args()
    if args.cmd is None:  # bare `cairn` still means `cairn run`
        args = parser.parse_args(["run"])
    paths.lock_down()
    ui.attach()
    if args.cmd in LOGGED_COMMANDS:
        logfile.attach(paths.run_log())
    try:
        code = args.func(args)
    except (settings.SettingsError, secrets.SecretsError, schedule.ScheduleError) as e:
        ui.error(str(e))
        code = 2
    sys.exit(code)


if __name__ == "__main__":
    main()
