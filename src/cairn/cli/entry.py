"""The entry point: parse the arguments and run the command."""
import io
import sys

from cairn.cli.parser import build_parser
from cairn.core import logfile, paths, secrets, settings, ui
from cairn.system import schedule

LOGGED_COMMANDS = {"run", "check", "serve", "icons"}

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
    parser = build_parser()
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
