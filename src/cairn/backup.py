"""Back the home directory up to one zip, and restore it from one."""
import contextlib
import datetime
import itertools
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import zipfile
import zlib
from pathlib import Path

from cairn import __version__, locks, paths, pipeline, settings, store

FORMAT = 1
MANIFEST = "manifest.json"
DATABASE = "pipeline.db"
LOGOS = "logos"
TEXT_FILES = ("config.toml", "profile.md")
# what a restore moves aside; the WAL files belong to the database they sit beside
HOME_ITEMS = (*TEXT_FILES, DATABASE, f"{DATABASE}-wal", f"{DATABASE}-shm", LOGOS)
COUNTED = ("postings", "active", "seen", "scored", "applied", "runs")
SERVER_MARKER = "server.pid"
ZIP_MAGIC = b"PK\x03\x04"
MAX_ZIP = 2**30
MAX_UNPACKED = 2 * 2**30
MAX_ENTRY = 2**30
MAX_TEXT_ENTRY = 16 * 2**20
MAX_RATIO = 100
# a small file of repeated bytes, such as a mostly empty 4 KiB page, packs far past
# the ratio without being a bomb
RATIO_FROM = 2**20
# what reading a zip entry raises: a corrupt or truncated entry, a full disk, and
# RuntimeError or NotImplementedError for encryption or a compression zipfile lacks
UNREADABLE = (OSError, EOFError, zipfile.BadZipFile, zlib.error, RuntimeError,
              NotImplementedError)
# the settings that choose which program or endpoint receives the profile
MODEL_SETTINGS = ("llm_provider", "llm_base_url", "claude_bin")


class BackupError(Exception):
    """A one-line reason a backup could not be read or restored."""


class ModelChange(BackupError):
    """A backup whose MODEL_SETTINGS differ from the current ones, restored without
    accept_model_change. changes holds {"setting", "current", "incoming"} dicts."""

    def __init__(self, changes):
        self.changes = changes
        shown = ", ".join(f"{c['setting']} {c['current']!r} to {c['incoming']!r}"
                          for c in changes)
        super().__init__(f"the backup changes the AI provider settings ({shown}). "
                         f"Confirm to restore it")


def _stamp(fmt):
    return datetime.datetime.now().strftime(fmt)


def _copy_database(dest):
    """Copy the live database to dest through the sqlite backup API, which reads one
    consistent snapshot while other connections keep writing."""
    target = sqlite3.connect(dest)
    try:
        store.connect().backup(target)
    finally:
        target.close()


def _manifest(files):
    counts = store.counts()
    return {"format": FORMAT, "version": __version__, "schema": counts["schema_version"],
            "created_at": datetime.datetime.now().isoformat(timespec="seconds"),
            "counts": {name: counts[name] for name in COUNTED}, "files": files}


def _publish(partial, dest_dir, stem):
    """Link partial to <stem>.zip in dest_dir, or <stem>-2.zip and on when taken."""
    for n in itertools.count(1):
        dest = dest_dir / (f"{stem}.zip" if n == 1 else f"{stem}-{n}.zip")
        try:
            os.link(partial, dest)
        except FileExistsError:
            continue
        partial.unlink()
        return dest


def export(dest_dir=None):
    """Write cairn-backup-<YYYYMMDD-HHMMSS>.zip to dest_dir, ~/Downloads by
    default, with -2, -3, ... added when the name is taken.

    Returns its path.
    """
    dest_dir = Path(dest_dir or "~/Downloads").expanduser()
    dest_dir.mkdir(parents=True, exist_ok=True)
    stem = f"cairn-backup-{_stamp('%Y%m%d-%H%M%S')}"
    home = paths.home()
    with tempfile.TemporaryDirectory() as scratch:
        database = Path(scratch) / DATABASE
        _copy_database(database)
        partial = dest_dir / f".{stem}.{os.getpid()}.partial"
        files = []
        try:
            fd = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as out, \
                    zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as bundle:
                for name in TEXT_FILES:
                    if (home / name).is_file():
                        bundle.write(home / name, name)
                        files.append(name)
                bundle.write(database, DATABASE)
                files.append(DATABASE)
                logos = home / LOGOS
                for icon in sorted(logos.iterdir()) if logos.is_dir() else ():
                    if icon.is_file() and not icon.name.startswith("."):
                        bundle.write(icon, f"{LOGOS}/{icon.name}")
                        files.append(f"{LOGOS}/{icon.name}")
                bundle.writestr(MANIFEST, json.dumps(_manifest(files), indent=2))
            return _publish(partial, dest_dir, stem)
        finally:
            partial.unlink(missing_ok=True)


def export_settings():
    """The text of config.toml. Raises FileNotFoundError when there is none."""
    return paths.config_file().read_text(encoding="utf-8")


def _allowed(name):
    if name in (*TEXT_FILES, DATABASE, MANIFEST):
        return True
    folder, _, icon = name.partition("/")
    return (folder == LOGOS and icon not in ("", ".", "..") and "/" not in icon
            and "\\" not in icon)


def _size(n):
    return f"{n // 2**30} GiB" if n >= 2**30 else f"{n // 2**20} MiB"


def _check_sizes(bundle):
    """Refuse a zip whose entries claim more than a backup can hold once unpacked.

    zipfile stops reading an entry at its declared file_size, so the declared sizes
    bound what extraction writes.
    """
    total = 0
    for info in bundle.infolist():
        name, size = info.filename, info.file_size
        cap = MAX_TEXT_ENTRY if name in (*TEXT_FILES, MANIFEST) else MAX_ENTRY
        if size > cap:
            raise BackupError(f"this backup is too large to restore. Its {name} expands to "
                              f"over {_size(cap)}")
        if size >= RATIO_FROM and size > MAX_RATIO * max(info.compress_size, 1):
            raise BackupError(f"this backup is too large to restore. Its {name} expands to over "
                              f"{MAX_RATIO} times its size in the backup")
        total += size
    if total > MAX_UNPACKED:
        raise BackupError(f"this backup is too large to restore. It expands to over "
                          f"{_size(MAX_UNPACKED)}")


def read_manifest(bundle):
    """The manifest of an open backup zip, checked against this version's schema."""
    names = bundle.namelist()
    strays = [name for name in names if not _allowed(name)]
    if strays:
        raise BackupError("this file isn't a Cairn backup")
    if len(set(names)) != len(names):
        raise BackupError("this file isn't a Cairn backup")
    if MANIFEST not in names or DATABASE not in names:
        raise BackupError("this file isn't a Cairn backup")
    _check_sizes(bundle)
    try:
        manifest = json.loads(bundle.read(MANIFEST))
        schema = manifest["schema"]
    except (ValueError, KeyError, TypeError, *UNREADABLE):
        raise BackupError("this file isn't a Cairn backup") from None
    if not isinstance(schema, int) or isinstance(schema, bool):
        raise BackupError("this file isn't a Cairn backup")
    if schema > store.SCHEMA_VERSION:
        raise BackupError(
            "this backup comes from a newer version of Cairn. Update Cairn, then restore again")
    return manifest


def _check_database(path, schema):
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            found = int(conn.execute(
                "SELECT value FROM meta WHERE key = 'schema_version'").fetchone()[0])
        finally:
            conn.close()
    except (sqlite3.Error, TypeError, ValueError):
        raise BackupError("this backup is damaged. Try another backup") from None
    if found != schema:
        raise BackupError("this backup is damaged. Try another backup")


def _check_config(path):
    """The backup's settings, every default when it has no config.toml."""
    try:
        return settings.load(path)
    except settings.SettingsError:
        raise BackupError("this backup's settings don't work with this version of Cairn. "
                          "Update Cairn, or try another backup") from None


def _model_changes(incoming):
    """The MODEL_SETTINGS that incoming changes. Every one counts as changed, from
    None, when the current config.toml cannot be loaded."""
    try:
        current = settings.base()
    except settings.SettingsError:
        return [{"setting": name, "current": None, "incoming": getattr(incoming, name)}
                for name in MODEL_SETTINGS]
    return [{"setting": name, "current": getattr(current, name),
             "incoming": getattr(incoming, name)}
            for name in MODEL_SETTINGS if getattr(current, name) != getattr(incoming, name)]


def _extract(bundle, staged):
    """Unpack every entry but the manifest into staged, readable by the owner only,
    since profile.md holds a resume."""
    for info in bundle.infolist():
        if info.filename == MANIFEST:
            continue
        dest = staged / info.filename
        dest.parent.mkdir(mode=0o700, exist_ok=True)
        try:
            fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as out, bundle.open(info) as packed:
                shutil.copyfileobj(packed, out)
        except UNREADABLE:
            raise BackupError("Cairn couldn't copy the backup's files. Check that your "
                              "computer has free space and try again") from None


def _aside_dir(home):
    """A new, empty <home>.bak-<stamp> beside home."""
    stamp = _stamp("%Y%m%d-%H%M%S-%f")
    for n in itertools.count(1):
        aside = home.with_name(f"{home.name}.bak-{stamp}" + ("" if n == 1 else f"-{n}"))
        try:
            aside.mkdir(mode=0o700)
        except FileExistsError:
            continue
        return aside


def _swap(staged, home, aside):
    """Move home's items into aside, then the staged ones into home. On a failure
    the moves are undone, and a BackupError names aside only when that fails too."""
    moved, placed = [], []
    try:
        for name in HOME_ITEMS:
            if os.path.lexists(home / name):
                os.replace(home / name, aside / name)
                moved.append(name)
        for item in sorted(staged.iterdir()):
            os.replace(item, home / item.name)
            placed.append(item.name)
    except OSError:
        try:
            for name in reversed(placed):
                os.replace(home / name, staged / name)
            for name in reversed(moved):
                os.replace(aside / name, home / name)
        except OSError:
            raise BackupError(f"the restore failed and Cairn couldn't put your data back. "
                              f"Your previous data is in {aside}") from None
        aside.rmdir()
        raise BackupError("the restore failed, and your data is back as it was. Check that "
                          "your computer has free space and try again") from None


def _open(zip_file):
    """The ZipFile for a path or binary file, refused unless it is a zip of at most
    MAX_ZIP bytes."""
    try:
        if isinstance(zip_file, (str, os.PathLike)):
            size = os.stat(zip_file).st_size
            with open(zip_file, "rb") as f:
                head = f.read(len(ZIP_MAGIC))
        else:
            size = zip_file.seek(0, os.SEEK_END)
            zip_file.seek(0)
            head = zip_file.read(len(ZIP_MAGIC))
            zip_file.seek(0)
    except OSError:
        raise BackupError("Cairn couldn't open this file. Try another backup") from None
    if size > MAX_ZIP:
        raise BackupError(f"this file is over {_size(MAX_ZIP)}, too large to be a Cairn backup")
    if head != ZIP_MAGIC:
        raise BackupError("this file isn't a Cairn backup")
    try:
        return zipfile.ZipFile(zip_file)
    except (OSError, zipfile.BadZipFile):
        raise BackupError("Cairn couldn't open this file. Try another backup") from None


def restore(zip_file, accept_model_change=False):
    """Replace the home's config, profile, database, and icons with those in a backup
    zip, given as a path or a binary file.

    The current ones move to a new <home>.bak-<stamp>/ first, and that path is
    returned. The folders earlier restores left stay.
    Raises BackupError for a zip that is no backup, is too large, is from a newer
    schema, or could not be swapped in, ModelChange for one that changes
    MODEL_SETTINGS unless accept_model_change, and pipeline.RunInProgress while a run
    holds the lock.
    """
    home = paths.home()
    bundle = _open(zip_file)
    with bundle, pipeline.run_lock():
        manifest = read_manifest(bundle)
        # staged beside the home so every move below is a rename on one file system
        with tempfile.TemporaryDirectory(dir=home.parent,
                                         prefix=f".{home.name}.restore-") as staged:
            staged = Path(staged)
            _extract(bundle, staged)
            _check_database(staged / DATABASE, manifest["schema"])
            changes = _model_changes(_check_config(staged / "config.toml"))
            if changes and not accept_model_change:
                raise ModelChange(changes)
            aside = _aside_dir(home)
            store.close()
            _swap(staged, home, aside)
    settings.reset()
    return aside


def server_marker():
    return paths.home() / SERVER_MARKER


@contextlib.contextmanager
def serving():
    """Hold server.pid, shared with any other app server, while this one runs, so a
    restore from the command line can tell the app is open."""
    marker = server_marker()
    marker.parent.mkdir(parents=True, exist_ok=True)
    fd = locks.open_file(marker, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        locks.acquire(fd, shared=True, wait=True)
        locks.write_pid(fd)
        try:
            yield
        finally:
            locks.release(fd)
            # another server that still holds it keeps the file. Windows can't delete
            # a file this process has open, and the lock alone marks a running server.
            if locks.acquire(fd):
                if sys.platform != "win32":
                    marker.unlink(missing_ok=True)
                locks.release(fd)
    finally:
        os.close(fd)


def app_running():
    """Whether an app server holds server.pid, in this process or another."""
    try:
        fd = locks.open_file(server_marker(), os.O_RDONLY)
    except FileNotFoundError:
        return False
    try:
        if not locks.acquire(fd):
            return True
        locks.release(fd)
        return False
    finally:
        os.close(fd)
