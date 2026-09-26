"""Every file the pipeline reads or writes, under one home directory.

CAIRN_HOME points the whole pipeline somewhere else, which is how the tests
run against a temp directory.
"""
import os
import stat
from pathlib import Path


def home():
    return Path(os.environ.get("CAIRN_HOME") or "~/.cairn").expanduser()


def config_file():
    return home() / "config.toml"


def profile_md():
    return home() / "profile.md"


def db_file():
    return home() / "pipeline.db"


def state_dir():
    return home() / "state"


def run_log():
    return home() / "run.log"


def run_lock():
    return home() / "run.lock"


def lock_down():
    """Create every file from here on readable by the owner alone, and take group and
    other access off the home and each entry at its top level.

    A home made by an earlier version is 0755 with 0644 files, profile.md and the
    database among them. Each directory inside it is closed by its own mode, so the
    walk stops at the top level.
    """
    os.umask(0o077)
    root = home()
    if not root.is_dir():
        return
    _owner_only(root)
    for entry in root.iterdir():
        if not entry.is_symlink():
            _owner_only(entry)


def _owner_only(path):
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
        if mode & 0o077:
            path.chmod(mode & 0o700)
    except FileNotFoundError:
        pass  # a lock or WAL file another process removed meanwhile
