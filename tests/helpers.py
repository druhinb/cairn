"""Fixtures shared by the test modules, and the suite-wide settings guard.

`unittest discover -s tests` imports each test module as a top-level module, so
tests/__init__.py never runs. The guard lives in this file, which every test
module imports; at import it points CAIRN_HOME at an empty temp
directory and installs the default settings. No test ever reads the developer's
real ~/.cairn/config.toml or sends a notification from it.
"""
import atexit
import contextlib
import dataclasses
import os
import shutil
import tempfile
from pathlib import Path

from fastapi.testclient import TestClient

from cairn import settings, store
from cairn.server import app

SUITE_HOME = tempfile.mkdtemp(prefix="cairn-tests-")
atexit.register(shutil.rmtree, SUITE_HOME, ignore_errors=True)
atexit.register(store.close)  # atexit is LIFO, so this runs before the rmtree
os.environ["CAIRN_HOME"] = SUITE_HOME
settings.use(settings.defaults())


@contextlib.contextmanager
def temp_home(**overrides):
    """Run a test against its own empty CAIRN_HOME with the default settings.

    Yields the home directory. Keyword arguments replace individual settings for the
    duration. On exit the suite baseline comes back: the suite home and a fresh
    `settings.defaults()`, so nothing is ever loaded from disk. Each call gets its
    own empty database.
    """
    with tempfile.TemporaryDirectory() as home:
        try:
            os.environ["CAIRN_HOME"] = home
            settings.use(dataclasses.replace(settings.defaults(), **overrides))
            yield Path(home)
        finally:
            store.close()
            os.environ["CAIRN_HOME"] = SUITE_HOME
            settings.use(settings.defaults())


def app_client(case, **kwargs):
    """A TestClient for a new app that passes as the app's own window: on this Mac,
    addressed to 127.0.0.1, sending the page's header, and holding the session cookie
    the launch URL buys."""
    client = case.enterContext(TestClient(
        app.create_app(), base_url="http://127.0.0.1", client=("127.0.0.1", 50000),
        headers={app.APP_HEADER: "1"}, **kwargs))
    client.get(f"/?{app.TOKEN_PARAM}={app.SESSION}")
    return client
