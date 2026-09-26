"""The app bundle's entry point: `cairn ui`, or the command it is given.

launchd starts the bundle with arguments for the daily run and the login item.
"""
import os
import sys
from pathlib import Path

from cairn import cli, schedule

# an app opened from the Finder inherits launchd's PATH, which lacks claude
os.environ["PATH"] = (schedule.LAUNCHD_PATH.format(home=Path.home())
                      + os.pathsep + os.environ.get("PATH", ""))
if len(sys.argv) == 1:
    sys.argv.append("ui")
cli.main()
