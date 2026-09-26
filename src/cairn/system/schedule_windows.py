"""The schedule on Windows: a Task Scheduler task for the daily `cairn run`, and an
entry under the Run key that opens `cairn ui` at login.

Both start pythonw.exe, which runs without a console window. The task definition is
kept in the data home as the file Task Scheduler was given, the way launchd keeps a
plist, and status() reads the time back from it.
"""
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from xml.sax.saxutils import escape

from cairn.core import paths
from cairn.system.schedule import DEFAULT_HOUR, DEFAULT_MINUTE, ScheduleError

TASK = "Cairn daily run"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE = "Cairn"
TIMEOUT = 10  # seconds for any schtasks call
TASK_NS = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
# StartWhenAvailable runs a time the computer slept through once it wakes, as
# launchd does on a Mac
TASK_XML = """<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="{ns}">
  <Triggers>
    <CalendarTrigger>
      <StartBoundary>2026-01-01T{hour:02d}:{minute:02d}:00</StartBoundary>
      <ScheduleByDay><DaysInterval>1</DaysInterval></ScheduleByDay>
    </CalendarTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author"><LogonType>InteractiveToken</LogonType></Principal>
  </Principals>
  <Settings>
    <StartWhenAvailable>true</StartWhenAvailable>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{program}</Command>
      <Arguments>-m cairn.cli run</Arguments>
      <WorkingDirectory>{home}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""


def task_file():
    return paths.home() / "daily-task.xml"


def program():
    """The pythonw.exe beside the Python running Cairn."""
    found = Path(sys.executable).with_name("pythonw.exe")
    if not found.exists():
        raise ScheduleError(f"no pythonw.exe beside {sys.executable}; reinstall Cairn "
                            "with `uv tool install`, then try again")
    return found


def render(program_path, hour=DEFAULT_HOUR, minute=DEFAULT_MINUTE):
    """The Task Scheduler XML for a task running program_path daily at hour:minute."""
    return TASK_XML.format(ns=TASK_NS["t"], hour=int(hour), minute=int(minute),
                           program=escape(str(program_path)), home=escape(str(paths.home())))


def _schtasks(*args):
    try:
        return subprocess.run(["schtasks", *map(str, args)], capture_output=True,
                              text=True, encoding="oem", errors="replace",
                              timeout=TIMEOUT)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise ScheduleError(f"schtasks {args[0]} failed: {e}") from None


def _checked(*args):
    out = _schtasks(*args)
    if out.returncode != 0:
        reason = (out.stderr or out.stdout).strip().splitlines() or [f"exit {out.returncode}"]
        raise ScheduleError(f"schtasks {args[0]} failed: {reason[-1]}")
    return out.stdout


def _registered():
    return _schtasks("/Query", "/TN", TASK).returncode == 0


def install(hour=DEFAULT_HOUR, minute=DEFAULT_MINUTE):
    """Write the task definition and register it, replacing any earlier one.
    Returns status()."""
    text = render(program(), hour, minute)
    path = task_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    # schtasks reads a definition file as UTF-16 and turns down UTF-8
    path.write_text(text, encoding="utf-16")
    _checked("/Create", "/TN", TASK, "/XML", path, "/F")
    return status()


def remove():
    """Unregister the task and delete its definition. Returns status()."""
    if _registered():
        _checked("/Delete", "/TN", TASK, "/F")
    task_file().unlink(missing_ok=True)
    return status()


def _read(path):
    """{program, hour, minute} from a task definition."""
    task = ET.parse(path).getroot()
    command = task.findtext("t:Actions/t:Exec/t:Command", namespaces=TASK_NS)
    start = task.findtext("t:Triggers/t:CalendarTrigger/t:StartBoundary",
                          namespaces=TASK_NS) or ""
    clock = start.partition("T")[2].split(":")
    hour, minute = (int(clock[0]), int(clock[1])) if len(clock) >= 2 else (None, None)
    return {"program": command, "hour": hour, "minute": minute}


def _one_line(e):
    return (str(e).strip().splitlines() or [type(e).__name__])[0]


def status():
    """{installed, loaded, plist, program, hour, minute, error} for the daily task,
    where plist names the task. Never raises."""
    path = task_file()
    result = {"installed": path.exists(), "loaded": False, "plist": TASK,
              "program": None, "hour": None, "minute": None, "error": ""}
    try:
        result["loaded"] = _registered()
    except ScheduleError as e:
        result["error"] = str(e)
    if result["installed"]:
        try:
            result.update(_read(path))
        except Exception as e:  # noqa: BLE001 - any malformed definition is reported, not raised
            result["error"] = f"cannot read {path}: {type(e).__name__}: {_one_line(e)}"
    return result


def _winreg():
    import winreg  # noqa: PLC0415 - Windows only; the tests put a fake in sys.modules
    return winreg


def autostart_install():
    """Add the Run entry that opens the app at every login. Returns autostart_status()."""
    winreg = _winreg()
    command = f'"{program()}" -m cairn.cli ui'
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
        winreg.SetValueEx(key, RUN_VALUE, 0, winreg.REG_SZ, command)
    return autostart_status()


def autostart_remove():
    """Delete the Run entry. Returns autostart_status()."""
    winreg = _winreg()
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                            winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, RUN_VALUE)
    except FileNotFoundError:
        pass  # absent is the state remove is asked for
    return autostart_status()


def autostart_status():
    """{installed, loaded, plist, program, error} for the Run entry, where plist names
    the entry. Windows reads the Run key at each login, so an entry is loaded as soon
    as it is there. Never raises."""
    winreg = _winreg()
    result = {"installed": False, "loaded": False,
              "plist": rf"HKEY_CURRENT_USER\{RUN_KEY}\{RUN_VALUE}", "program": None,
              "error": ""}
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            command, _ = winreg.QueryValueEx(key, RUN_VALUE)
    except FileNotFoundError:
        return result
    except OSError as e:
        result["error"] = f"cannot read the Run key: {_one_line(e)}"
        return result
    program_path = command.split('"')[1] if command.startswith('"') else command.split(" ")[0]
    result.update(installed=True, loaded=True, program=program_path)
    return result
