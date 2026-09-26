"""Every cairn command but init, one cmd_ function each."""
import argparse
import getpass
import sys
import time
from pathlib import Path

from cairn import store
from cairn.ai import claude, llm
from cairn.core import paths, secrets, settings, ui
from cairn.jobs import fetch, logos, notify, pipeline
from cairn.system import backup, doctor, schedule, update
from cairn.tracking import applications

TOP_SHOWN = 15  # ranked postings echoed to the terminal; the app has them all

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
