"""The local web app, served by uvicorn from the CLI or from a thread in the window."""
import socket
import threading
import time
import webbrowser

import uvicorn

from cairn.core import ui
from cairn.server.app import create_app
from cairn.server.security import SESSION, TOKEN_PARAM


def pick_port(host="127.0.0.1"):
    """A port that was free a moment ago."""
    with socket.socket() as s:
        s.bind((host, 0))
        return s.getsockname()[1]


def page_url(host, port):
    """The URL that opens the app. Its token trades for the session cookie, so only
    whoever holds this URL gets in."""
    netloc = f"[{host}]" if ":" in host else host
    return f"http://{netloc}:{port}/?{TOKEN_PARAM}={SESSION}"


def start_in_thread(host, port):
    """(server, thread): uvicorn serving the app on a daemon thread.

    Returns once the server accepts connections. Set server.should_exit and join the
    thread to stop it. Off the main thread uvicorn installs no signal handlers.
    Raises OSError when the address cannot be bound.
    """
    # bound here so a port in use raises in the caller; uvicorn would log and exit
    # its own thread
    sock = socket.create_server((host, port))
    app = create_app()
    server = uvicorn.Server(uvicorn.Config(app, host=host, port=port, log_level="warning"))
    app.state.shutting_down = lambda: server.should_exit
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True,
                              name="cairn-server")
    thread.start()
    while not server.started:
        if not thread.is_alive():
            sock.close()
            raise RuntimeError(f"the server did not start on {host}:{port}")
        time.sleep(0.05)
    return server, thread


def serve(host="127.0.0.1", port=None, open_browser=False):
    """Serve until interrupted. Without a port, any free one is used.

    Raises OSError or RuntimeError when the server cannot start.
    """
    port = port or pick_port(host)
    server, thread = start_in_thread(host, port)
    url = page_url(host, port)
    ui.info(url)
    if open_browser:
        webbrowser.open(url)
    while thread.is_alive():
        try:
            thread.join(0.5)
        except KeyboardInterrupt:
            # a second Ctrl-C stops without waiting for open requests
            server.force_exit = server.should_exit
            server.should_exit = True
