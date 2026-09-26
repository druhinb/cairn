"""`cairn ui`: the web app in a native window, or in the browser.

pywebview on macOS must own the main thread, so uvicorn serves from a daemon thread
and the window blocks the main one until it is closed.
"""
import ctypes
import sys
import time
import urllib.request
import webbrowser

from cairn import icon, logfile, menubar, paths, server, ui
from cairn.server.app import own_headers

HOST = "127.0.0.1"
READY_TIMEOUT = 5  # seconds the server gets to answer /api/status
STOP_TIMEOUT = 10  # seconds to wait for the server thread after the window closes
DOCK_ICON_SIZE = 512
APP_NAME = "Cairn"
CORE_FOUNDATION = "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"
CORE_SERVICES = "/System/Library/Frameworks/CoreServices.framework/CoreServices"


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


def _present_as_cairn():
    """Name the app Cairn and return the path of its Dock icon, or None when the
    system already has both. Run from a plain Python, macOS shows the
    interpreter's name, such as python3.13, and a generic icon; Cairn.app gets both
    from its Info.plist."""
    if sys.platform != "darwin" or getattr(sys, "frozen", False):
        return None
    from AppKit import NSApplication, NSApplicationActivationPolicyRegular  # noqa: PLC0415 - see _show_window
    from Foundation import NSBundle  # noqa: PLC0415
    # pywebview's Hide and Quit menu items read the name from here
    NSBundle.mainBundle().infoDictionary()["CFBundleName"] = APP_NAME
    NSApplication.sharedApplication().setActivationPolicy_(NSApplicationActivationPolicyRegular)
    try:
        _rename_in_launch_services(APP_NAME)
    except (AttributeError, OSError, ValueError) as e:
        ui.warn(f"the Dock keeps showing the Python name: {e}")
    path = paths.home() / "icon.png"
    path.write_bytes(icon.png(DOCK_ICON_SIZE))
    return str(path)


def _rename_in_launch_services(name):
    """Set the name the Dock, the app menu and the app switcher show for this
    process. LaunchServices has no public call for this; Chromium names its
    helper processes with the same private one."""
    foundation = ctypes.CDLL(CORE_FOUNDATION)
    foundation.CFStringCreateWithCString.restype = ctypes.c_void_p
    foundation.CFStringCreateWithCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p,
                                                     ctypes.c_uint32]
    foundation.CFRelease.argtypes = [ctypes.c_void_p]
    services = ctypes.CDLL(CORE_SERVICES)
    services._LSGetCurrentApplicationASN.restype = ctypes.c_void_p
    services._LSSetApplicationInformationItem.restype = ctypes.c_int
    services._LSSetApplicationInformationItem.argtypes = [ctypes.c_int] + [ctypes.c_void_p] * 4
    display_name_key = ctypes.c_void_p.in_dll(services, "_kLSDisplayNameKey").value
    value = foundation.CFStringCreateWithCString(None, name.encode(), 0x08000100)  # UTF-8
    try:
        status = services._LSSetApplicationInformationItem(
            -2, services._LSGetCurrentApplicationASN(), display_name_key, value, None)  # -2: this session
    finally:
        foundation.CFRelease(value)
    if status:
        raise OSError(f"LaunchServices refused the name (status {status})")


def _show_window(url, page):
    """Show page in the window until Quit, and on a Mac the menu-bar item that calls
    the API at url, where closing the window hides it."""
    dock_icon = _present_as_cairn()
    import webview  # noqa: PLC0415 - needs a display; --no-window and the tests run without one
    window = webview.create_window("Cairn", page, width=1280, height=860,
                                   min_size=(900, 600))
    bar = menubar.attach(url, window) if sys.platform == "darwin" else None
    webview.start(bar.started if bar else None, icon=dock_icon)
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
