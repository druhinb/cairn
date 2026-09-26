"""The command line: every command, its options, and the checks on their values."""
import argparse
import sys

from cairn import store
from cairn.ai import llm
from cairn.cli import commands, init
from cairn.cli.commands import LOCAL_HOSTS
from cairn.core import paths, settings, ui
from cairn.system import schedule

DESCRIPTION = """Daily new-grad job feed.

Fetches postings, ranks them for fit and company tier, and summarises the
requirements of the top matches. A run marks postings seen only after it stores
their scores, so a failed run retries next time.
"""


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


def build_parser():
    kwargs = {}
    if sys.version_info >= (3, 14):
        # Python 3.14's argparse colourises its own usage and error output, which
        # ui.py cannot intercept. Left on, a bad argument under launchd writes raw
        # escapes into launchd.err.
        kwargs["color"] = ui.IS_TTY
    parser = argparse.ArgumentParser(
        prog="cairn", description=DESCRIPTION,
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
    init_p.set_defaults(func=init.cmd_init)

    run = sub.add_parser("run", help="fetch, rank, and summarize new postings")
    run.add_argument("--limit", type=_positive, metavar="N",
                     help="process only the N newest postings and leave the rest unseen")
    run.add_argument("--dry-run", action="store_true",
                     help="list the postings a run would process, without ranking them or marking them seen")
    run.add_argument("--fit", type=int, metavar="N",
                     help="fit score needed for a summary and the push (default "
                          f"{settings.defaults().fit_threshold}). Applies to this "
                          "run only and leaves config.toml unchanged")
    run.set_defaults(func=commands.cmd_run)

    fetch_p = sub.add_parser("fetch", help="dry run: show new postings, rank nothing")
    fetch_p.add_argument("--limit", type=_positive, default=25, metavar="N",
                         help="how many postings to list (default 25)")
    fetch_p.set_defaults(func=commands.cmd_fetch)

    seed_p = sub.add_parser("seed", help="mark the current backlog as seen (run once)")
    seed_p.set_defaults(func=commands.cmd_seed)

    icons_p = sub.add_parser("icons", help="look up company websites and download their icons")
    icons_p.add_argument("--limit", type=_positive, metavar="N",
                         help="look up at most N companies (default: every one)")
    icons_p.add_argument("--reset-failures", action="store_true",
                         help="retry the companies whose icon lookup failed before")
    icons_p.set_defaults(func=commands.cmd_icons)

    status_p = sub.add_parser("status", help="postings stored, applications logged, last run")
    status_p.set_defaults(func=commands.cmd_status)

    applied_p = sub.add_parser("applied", help="log that you applied to a posting")
    applied_p.add_argument("job_id", metavar="ID", help="posting id, as run prints it")
    applied_p.add_argument("--note", default="", help="a note to keep with the posting")
    applied_p.set_defaults(func=commands.cmd_applied)

    track_p = sub.add_parser("track", help="set where a posting stands, or --clear it")
    track_p.add_argument("job_id", metavar="ID", help="posting id, as run prints it")
    track_p.add_argument("status", nargs="?", choices=store.STATUSES,
                         help="saved, applied, interviewing, offer, rejected, withdrawn, "
                              "or passed (not interested)")
    track_p.add_argument("--note", help="replaces the stored note")
    track_p.add_argument("--clear", action="store_true",
                         help="forget the posting's status, note, and history")
    track_p.set_defaults(func=commands.cmd_track)

    serve_p = sub.add_parser("serve", help="run the local web app")
    serve_p.add_argument("--host", default="127.0.0.1", choices=LOCAL_HOSTS,
                         help="loopback address to listen on (default 127.0.0.1)")
    serve_p.add_argument("--port", type=_positive, metavar="N",
                         help="port to listen on (default: any free port)")
    serve_p.set_defaults(func=commands.cmd_serve)

    ui_p = sub.add_parser("ui", help="open the app in a window")
    ui_p.add_argument("--no-window", action="store_true",
                      help="open it in the browser instead, and serve until Ctrl-C")
    ui_p.add_argument("--port", type=_positive, metavar="N",
                      help="port to serve on (default: any free port)")
    ui_p.set_defaults(func=commands.cmd_ui)

    doctor_p = sub.add_parser("doctor", help="check that this machine can run the pipeline")
    doctor_p.set_defaults(func=commands.cmd_doctor)

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
    schedule_p.set_defaults(func=commands.cmd_schedule)

    autostart_p = sub.add_parser("autostart", help="open the app at login, or stop that")
    autostart_actions = autostart_p.add_subparsers(dest="action", required=True,
                                                   metavar="{install,remove,status}")
    autostart_actions.add_parser("install", help="open the app at every login")
    autostart_actions.add_parser("remove", help="stop opening the app at login")
    autostart_actions.add_parser("status", help="show whether the app opens at login")
    autostart_p.set_defaults(func=commands.cmd_autostart)

    update_p = sub.add_parser("update-check", help="check GitHub for a newer release")
    update_p.add_argument("--force", action="store_true",
                          help="ask GitHub even when it was asked in the last 24 hours")
    update_p.set_defaults(func=commands.cmd_update_check)

    backup_p = sub.add_parser("backup", help="save config, profile, database, and icons "
                                             "to one zip")
    backup_p.add_argument("--to", metavar="DIR", help="folder for the zip (default ~/Downloads)")
    backup_p.set_defaults(func=commands.cmd_backup)

    restore_p = sub.add_parser("restore", help="replace your data with a backup's")
    restore_p.add_argument("zip", metavar="ZIP", help="a zip that `cairn backup` wrote")
    restore_p.add_argument("--yes", action="store_true", help="restore without asking")
    restore_p.set_defaults(func=commands.cmd_restore)

    settings_p = sub.add_parser("settings", help="print config.toml")
    settings_actions = settings_p.add_subparsers(dest="action", required=True,
                                                 metavar="{export}")
    settings_actions.add_parser("export", help="print config.toml to standard output")
    settings_p.set_defaults(func=commands.cmd_settings_export)

    llm_p = sub.add_parser("llm", help="show, set, or test the model provider")
    llm_actions = llm_p.add_subparsers(dest="action", metavar="{set,test}")
    set_p = llm_actions.add_parser("set", help="choose a provider and store its key")
    set_p.add_argument("provider", metavar="PROVIDER", help=", ".join(llm.PROVIDERS))
    set_p.add_argument("--model", help="the model for ranking (empty uses the provider's default)")
    set_p.add_argument("--cheap-model", help="the model for summaries")
    set_p.add_argument("--base-url", help="the endpoint, for ollama and custom")
    set_p.add_argument("--key-stdin", action="store_true", help="read the API key from stdin")
    llm_actions.add_parser("test", help="send one short prompt and time the answer")
    llm_p.set_defaults(func=commands.cmd_llm)
    return parser
