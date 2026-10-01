"""One SQLite connection per thread, opened and migrated on first use."""
import _sqlite3
import ctypes
import datetime
import sqlite3
import sys
import threading

from cairn.core import paths, settings, ui
from cairn.jobs import places
from cairn.store.keys import name_key
from cairn.store.rows import _list
from cairn.store.schema import _AFTER_MIGRATION, SCHEMA, SCHEMA_VERSION, _migrate

# One connection per (path, thread). A shared connection would let a background
# run's `with conn:` transaction absorb a server handler's writes. A server
# request thread keeps its connection for as long as the pool keeps the thread,
# which anyio's thread limiter bounds; background job threads call close_thread().
_connections = {}
_connections_lock = threading.Lock()
# two threads opening a new database at once can fail the WAL switch or the schema
# setup with "database is locked" before the busy timeout runs out
_opening_lock = threading.Lock()

_SQLITE_CONFIG_MEMSTATUS = 9


def sqlite_library():
    """The SQLite library the sqlite3 module runs on."""
    if sys.platform == "win32":
        # _sqlite3.pyd exports none of SQLite; a bare name finds the sqlite3.dll it
        # already loaded
        return ctypes.CDLL("sqlite3")
    # uv's Pythons build _sqlite3 into the interpreter, which CDLL(None) opens
    return ctypes.CDLL(getattr(_sqlite3, "__file__", None))


def _stop_memory_stats():
    """Turn off SQLite's allocation statistics, which put every allocation of every
    thread behind one mutex. Four request threads each took 1,030 ms over a query
    that took 80 ms alone.

    SQLite takes the setting only while shut down, and the sqlite3 module starts it
    on import. Shutting it down with a connection open is undefined, so this runs
    once, at import, before this module has opened any.
    """
    try:
        lib = sqlite_library()
        config = lib.sqlite3_config
        # the option is the one fixed parameter; on arm64 the value after it is
        # read from the stack
        config.argtypes = [ctypes.c_int]
        lib.sqlite3_shutdown()
        configured = config(_SQLITE_CONFIG_MEMSTATUS, ctypes.c_int(0))
        started = lib.sqlite3_initialize()
    except (OSError, AttributeError) as e:
        ui.warn(f"[store] SQLite allocation statistics stay on, so parallel queries "
                f"queue: {type(e).__name__}: {e}")
        return
    if configured or started:
        ui.warn(f"[store] SQLite allocation statistics stay on, so parallel queries "
                f"queue: sqlite3_config returned {configured}, sqlite3_initialize "
                f"{started}")


_stop_memory_stats()


def _abroad(locations):
    return places.abroad(_list(locations))


def now():
    return datetime.datetime.now().isoformat(timespec="seconds")


def connect():
    """This thread's connection for the current home, opened, migrated, and imported
    on first use."""
    path = paths.db_file().resolve()
    key = (path, threading.get_ident())
    with _connections_lock:
        conn = _connections.get(key)
    if conn is not None:
        return conn
    with _opening_lock:
        conn = _open(path)
    with _connections_lock:
        _connections[key] = conn
    return conn


def _open(path):
    # these modules connect through this one, so a top-level import would be circular
    from cairn.store import legacy, relevance  # noqa: PLC0415
    from cairn.store.search import date_group  # noqa: PLC0415
    path.parent.mkdir(parents=True, exist_ok=True)
    # each connection is used only by its own thread; the flag lets close() run
    # from any thread
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    # a restored database is untrusted input: its schema may not call functions with
    # side effects, or rewrite sqlite_schema
    conn.execute("PRAGMA trusted_schema = OFF")
    if hasattr(conn, "setconfig"):  # Python 3.12+
        conn.setconfig(sqlite3.SQLITE_DBCONFIG_DEFENSIVE, True)
    conn.create_function("name_key", 1, name_key, deterministic=True)
    conn.create_function("abroad", 1, _abroad, deterministic=True)
    conn.create_function("date_group", 1, date_group)
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        conn.executescript(SCHEMA)
        with conn:
            conn.execute("INSERT OR IGNORE INTO meta VALUES ('schema_version', ?)",
                         (str(SCHEMA_VERSION),))
        _migrate(conn)
        conn.executescript(_AFTER_MIGRATION)
        legacy._import_legacy(conn, paths.state_dir())
        try:
            relevance._sync_relevance(conn, settings.base())
        except settings.SettingsError:
            # relevant_query tests the rule itself until the settings load, and every
            # settings.get() reports the error
            pass
    except BaseException:
        conn.close()
        raise
    return conn


def close():
    """Close every thread's connection to the current home's database."""
    path = paths.db_file().resolve()
    with _connections_lock:
        doomed = [_connections.pop(k) for k in list(_connections) if k[0] == path]
    for conn in doomed:
        conn.close()


def close_thread():
    """Close the calling thread's connections, whichever home they belong to."""
    ident = threading.get_ident()
    with _connections_lock:
        doomed = [_connections.pop(k) for k in list(_connections) if k[1] == ident]
    for conn in doomed:
        conn.close()
