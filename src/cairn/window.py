"""`cairn ui`: the web app in a native window, or in the browser.

pywebview on macOS must own the main thread, so uvicorn serves from a daemon thread
and the window blocks the main one until it is closed.
"""
import time
import urllib.request
import webbrowser

from cairn import logfile, menubar, paths, server, ui
from cairn.server.app import own_headers

HOST = "127.0.0.1"
READY_TIMEOUT = 5  # seconds the server gets to answer /api/status
STOP_TIMEOUT = 10  # seconds to wait for the server thread after the window closes


def _wait_until_ready(url):
    # the server is local, so the system proxy settings never apply
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    request = urllib.request.Request(f"{url}/api/status", headers=own_headers(url))
    deadline = time.monotonic() + READY_TIMEOUT
    while True:
        try:
            with opener.open(request, timeout=1):
                return
        except OSError as e:
            if time.monotonic() >= deadline:
                message = f"{url} did not answer within {READY_TIMEOUT}s: {e}"
                raise RuntimeError(message) from None
            time.sleep(0.1)


def _show_window(url, page):
    """Show page in the window, and the menu-bar item that calls the API at url, until
    Quit. Closing the window hides it."""
    import webview  # noqa: PLC0415 - needs a display; --no-window and the tests run without one
    window = webview.create_window("Cairn", page, width=1280, height=860,
                                   min_size=(900, 600))
    bar = menubar.attach(url, window)
    webview.start(bar.started if bar else None)
    if bar:
        bar.stop()


def _wait_for_interrupt():
    while True:
        time.sleep(1)


def open_ui(no_window=False, port=None):
    """Serve the app and show it until the window closes or Ctrl-C.

    Raises OSError or RuntimeError when the server cannot start.
    """
    paths.lock_down()
    port = port or server.pick_port(HOST)
    srv, thread = server.start_in_thread(HOST, port)
    url, page = f"http://{HOST}:{port}", server.page_url(HOST, port)
    stop_logging = None
    try:
        # runs started from the window log to run.log like runs started from the CLI
        stop_logging = logfile.attach(paths.run_log())
        _wait_until_ready(url)
        if no_window:
            ui.info(f"{page}  (Ctrl-C to stop)")
            webbrowser.open(page)
            _wait_for_interrupt()
        else:
            _show_window(url, page)
    except KeyboardInterrupt:
        pass
    finally:
        srv.should_exit = True
        thread.join(STOP_TIMEOUT)
        if stop_logging:
            stop_logging()
