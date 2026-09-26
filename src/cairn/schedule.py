"""The launchd jobs: the daily `cairn run`, and `cairn ui` at login.

Install, remove, and inspect either one. On Windows, schedule_windows supplies the
same six functions.
"""
import os
import plistlib
import shutil
import subprocess
import sys
from importlib import resources
from pathlib import Path
from string import Template
from xml.sax.saxutils import escape

from cairn import paths

LABEL = "app.cairn.daily"
UI_LABEL = "app.cairn.autostart"
DEFAULT_HOUR, DEFAULT_MINUTE = 7, 0
TIMEOUT = 10  # seconds for any launchctl call
LAUNCHD_PATH = ("/opt/homebrew/bin:/usr/local/bin:{home}/.local/bin:"
                "{home}/.claude/local:/usr/bin:/bin")


class ScheduleError(Exception):
    """A one-line reason the schedule could not be read or changed."""


def plist_path():
    return Path("~/Library/LaunchAgents").expanduser() / f"{LABEL}.plist"


def autostart_plist_path():
    return Path("~/Library/LaunchAgents").expanduser() / f"{UI_LABEL}.plist"


def program():
    """The cairn executable the job runs: this one, else the one on PATH.

    Inside the app bundle that is the bundle's own executable, which takes the same
    arguments. A bundle macOS runs from a randomised read-only copy, because it was
    opened where it was downloaded, has no path launchd could run later.
    """
    if getattr(sys, "frozen", False):
        if "/AppTranslocation/" in sys.executable:
            raise ScheduleError("macOS is running Cairn from a temporary copy; move "
                                "Cairn.app to /Applications and open it once, then "
                                "try again")
        return Path(sys.executable)
    argv0 = Path(sys.argv[0])
    if argv0.name == "cairn" and argv0.resolve().exists():
        return argv0.resolve()
    found = shutil.which("cairn")
    if found:
        return Path(found)
    raise ScheduleError("no cairn executable found; install one with "
                        "`uv tool install <path to cairn>`, then run this again")


def render(program_path, hour=DEFAULT_HOUR, minute=DEFAULT_MINUTE):
    """The plist text for a job running program_path daily at hour:minute."""
    home_env = ""
    if os.environ.get("CAIRN_HOME"):
        home_env = ("\n    <key>CAIRN_HOME</key>\n"
                    f"    <string>{escape(os.environ['CAIRN_HOME'])}</string>")
    template = (resources.files("cairn") / "launchd.plist.template").read_text(encoding="utf-8")
    return Template(template).substitute(
        label=LABEL, program=escape(str(program_path)),
        path=escape(LAUNCHD_PATH.format(home=Path.home())), home_env=home_env,
        hour=int(hour), minute=int(minute),
        stdout=escape(str(paths.home() / "launchd.out")),
        stderr=escape(str(paths.home() / "launchd.err")))


def _launchctl(*args):
    try:
        out = subprocess.run(["launchctl", *map(str, args)], capture_output=True,
                             text=True, timeout=TIMEOUT)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise ScheduleError(f"launchctl {args[0]} failed: {e}") from None
    # launchctl load reports some failures on stderr with exit status 0
    failed = out.returncode != 0 or out.stderr.strip().startswith("Load failed")
    if failed:
        reason = (out.stderr or out.stdout).strip().splitlines() or [f"exit {out.returncode}"]
        raise ScheduleError(f"launchctl {args[0]} failed: {reason[-1]}")
    return out.stdout


def _unload(*args):
    try:
        _launchctl(*args)
    except ScheduleError:
        pass  # not loaded is the state unload is asked for


def install(hour=DEFAULT_HOUR, minute=DEFAULT_MINUTE):
    """Write the plist and (re)load it. Returns status()."""
    text = render(program(), hour, minute)
    path = plist_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    # launchd opens the log files before the job starts, so their directory must exist
    paths.home().mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    _unload("unload", path)
    _launchctl("load", path)
    return status()


def remove():
    """Unload and delete the plist. Returns status()."""
    path = plist_path()
    if path.exists():
        _unload("unload", path)
    else:
        # the file can be deleted while launchd still has the job loaded
        _unload("remove", LABEL)
    path.unlink(missing_ok=True)
    return status()


def _listed(label):
    """The `launchctl list` row for label as [pid, status, label], or None."""
    # one "PID<tab>Status<tab>Label" row per loaded job; the pid is "-" while it is idle
    for line in _launchctl("list").splitlines():
        row = line.split("\t")
        if row[-1] == label:
            return row
    return None


def _loaded(label=LABEL):
    return _listed(label) is not None


def _started_by_login_item():
    """Whether this process is the one the login item started."""
    if os.environ.get("XPC_SERVICE_NAME") == UI_LABEL:
        return True
    row = _listed(UI_LABEL)
    return row is not None and row[0] == str(os.getpid())


def _read(path):
    """{program, hour, minute} from an installed plist."""
    with path.open("rb") as f:
        job = plistlib.load(f)
    interval = job.get("StartCalendarInterval") or {}
    if isinstance(interval, list):  # launchd accepts several times; the first is shown
        interval = interval[0] if interval else {}
    return {"program": (job.get("ProgramArguments") or [None])[0],
            "hour": interval.get("Hour"), "minute": interval.get("Minute")}


def _one_line(e):
    return (str(e).strip().splitlines() or [type(e).__name__])[0]


def status():
    """{installed, loaded, plist, program, hour, minute, error} for the daily job.

    Never raises: a plist or launchctl that cannot be read leaves the fields it
    would have filled at None and says why in error.
    """
    path = plist_path()
    result = {"installed": path.exists(), "loaded": False, "plist": str(path),
              "program": None, "hour": None, "minute": None, "error": ""}
    try:
        result["loaded"] = _loaded()
    except ScheduleError as e:
        result["error"] = str(e)
    if result["installed"]:
        try:
            result.update(_read(path))
        except Exception as e:  # noqa: BLE001 - any malformed plist is reported, not raised
            result["error"] = f"cannot read {path}: {type(e).__name__}: {_one_line(e)}"
    return result


def render_autostart(program_path):
    """The plist text for a job that opens the app at login."""
    env = {"PATH": LAUNCHD_PATH.format(home=Path.home())}
    if os.environ.get("CAIRN_HOME"):
        env["CAIRN_HOME"] = os.environ["CAIRN_HOME"]
    job = {"Label": UI_LABEL,
           "ProgramArguments": [str(program_path), "ui"],
           "EnvironmentVariables": env,
           "RunAtLoad": True,
           "KeepAlive": False,
           "StandardOutPath": str(paths.home() / "ui.out"),
           "StandardErrorPath": str(paths.home() / "ui.err")}
    return plistlib.dumps(job).decode()


def autostart_install():
    """Write the login item's plist. Returns autostart_status().

    launchd loads every plist in LaunchAgents at the next login. Loading it now would
    start a second copy of the app at once, because RunAtLoad is true.
    """
    text = render_autostart(program())
    path = autostart_plist_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    paths.home().mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return autostart_status()


def autostart_remove():
    """Unload and delete the login item's plist. Returns autostart_status().

    When this process is the app the login item opened, unloading would make launchd
    stop it, so only the plist goes, and launchd forgets the job at logout.
    """
    path = autostart_plist_path()
    if not _started_by_login_item():
        if path.exists():
            _unload("unload", path)
        else:
            _unload("remove", UI_LABEL)
    path.unlink(missing_ok=True)
    return autostart_status()


def autostart_status():
    """{installed, loaded, plist, program, error} for the login item. Never raises."""
    path = autostart_plist_path()
    result = {"installed": path.exists(), "loaded": False, "plist": str(path),
              "program": None, "error": ""}
    try:
        result["loaded"] = _loaded(UI_LABEL)
    except ScheduleError as e:
        result["error"] = str(e)
    if result["installed"]:
        try:
            with path.open("rb") as f:
                result["program"] = (plistlib.load(f).get("ProgramArguments") or [None])[0]
        except Exception as e:  # noqa: BLE001 - any malformed plist is reported, not raised
            result["error"] = f"cannot read {path}: {type(e).__name__}: {_one_line(e)}"
    return result


if sys.platform == "win32":
    from cairn.schedule_windows import (  # noqa: E402, F811 - they replace the launchd forms
        autostart_install, autostart_remove, autostart_status, install, remove, status)
