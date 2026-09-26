"""The windowed app's menu-bar item and dock badge.

pywebview's Cocoa loop owns the main thread, and every AppKit call below runs on it;
the poll and the slow actions run on threads and hand their results back through
AppHelper.callAfter.

The item replaces pywebview's app delegate with a subclass, through BrowserView's
AppDelegate and _shared_app_delegate. Both are private, so a pywebview without them
gets the window alone.
"""
import datetime
import json
import threading
import urllib.error
import urllib.request
import webbrowser

from cairn import events, mark, schedule, update
from cairn.server.app import own_headers

POLL_SECONDS = 30
PYWEBVIEW_HOOKS = ("AppDelegate", "_shared_app_delegate")
TIMEOUT = 5  # seconds for a request to the app's own server
ICON_MARGIN = 1  # pt around the mark's 16 pt grid in the 18 pt item


def _now():
    # the same form as the runs table's finished_at, so the two compare as text
    return datetime.datetime.now().isoformat(timespec="seconds")


class Unseen:
    """Postings ranked by runs that finished after the window was last in front."""

    def __init__(self, looked_at):
        self.looked_at = looked_at
        self._ranked = {}  # run id -> postings it ranked

    def add(self, run):
        """Count a run from /api/status latest_run once it has finished."""
        if run and run.get("finished_at") and run["finished_at"] > self.looked_at \
                and run.get("ranked"):
            self._ranked[run["id"]] = run["ranked"]

    def looked(self, at):
        self.looked_at = at
        self._ranked.clear()

    def count(self):
        return sum(self._ranked.values())


def unseen_label(count):
    return f"{count} new since you looked" if count else "Nothing new since you looked"


def badge(count):
    return str(count) if count else None


def update_label(found):
    if found is None:
        return "Couldn't check for updates"
    if found["newer"]:
        return f"Update to {found['latest']} available"
    return f"Cairn {found['current']} is up to date"


class _Local:
    """Requests to the app's own server, which the system proxy never serves."""

    def __init__(self, url):
        self.url = url
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def get(self, path):
        request = urllib.request.Request(f"{self.url}{path}", headers=own_headers(self.url))
        with self._opener.open(request, timeout=TIMEOUT) as response:
            return json.load(response)

    def post(self, path):
        request = urllib.request.Request(
            f"{self.url}{path}", data=b"{}", method="POST",
            headers={"Content-Type": "application/json", **own_headers(self.url)})
        with self._opener.open(request, timeout=TIMEOUT) as response:
            return json.load(response)


def _error_text(e):
    if isinstance(e, urllib.error.HTTPError):
        try:
            return json.load(e).get("error") or str(e)
        except ValueError:
            return str(e)
    return f"{type(e).__name__}: {e}"


_controller_class = None


def _controller():
    """The app delegate class. pyobjc refuses a second class of one name, so it is
    defined on first use and kept."""
    global _controller_class
    if _controller_class is not None:
        return _controller_class
    import objc  # noqa: PLC0415 - AppKit needs a GUI session; the CLI and tests never load it
    from webview.platforms.cocoa import BrowserView  # noqa: PLC0415

    class CairnAppDelegate(BrowserView.AppDelegate):
        """pywebview's delegate, plus the menu actions and the app's activity."""

        def applicationShouldTerminate_(self, app):
            self.menubar.quitting = True
            return objc.super(CairnAppDelegate, self).applicationShouldTerminate_(app)

        def applicationShouldHandleReopen_hasVisibleWindows_(self, app, visible):
            self.menubar.open()
            return True

        def applicationDidBecomeActive_(self, notification):
            self.menubar.looked()

        def open_(self, sender):
            self.menubar.open()

        def runNow_(self, sender):
            self.menubar.run_now()

        def toggleLogin_(self, sender):
            self.menubar.toggle_login()

        def checkUpdates_(self, sender):
            self.menubar.check_updates()

        def openUpdate_(self, sender):
            self.menubar.open_update()

        def quit_(self, sender):
            self.menubar.quit()

    _controller_class = CairnAppDelegate
    return _controller_class


class MenuBar:
    """The status item, its menu, and the dock badge for one pywebview window."""

    def __init__(self, url, window):
        import AppKit  # noqa: PLC0415 - see _controller
        from PyObjCTools import AppHelper  # noqa: PLC0415
        from webview.platforms.cocoa import BrowserView  # noqa: PLC0415
        self.appkit, self.call_after = AppKit, AppHelper.callAfter
        self.local = _Local(url)
        self.window = window
        self.quitting = False
        self.unseen = Unseen(_now())
        self.release = None
        self.unseen_item = None
        self._stop = threading.Event()
        self._refresh = threading.Event()
        self._unsubscribe = None
        self.delegate = _controller().alloc().init()
        self.delegate.menubar = self
        # pywebview installs this delegate when it creates the first window
        BrowserView._shared_app_delegate = self.delegate
        window.events.closing += self._closing

    def _closing(self):
        if self.quitting:
            return True
        self.window.hide()
        return False

    def started(self):
        """pywebview's start callback, on a thread once the Cocoa loop runs."""
        self.call_after(self._build)
        # the server runs in this process, so a finished run reaches the badge at once
        self._unsubscribe = events.subscribe(self._on_event)
        threading.Thread(target=self._poll, daemon=True, name="cairn-menubar").start()
        self._in_background(update.check, self._show_update)
        self._in_background(schedule.autostart_status, self._show_login)

    def stop(self):
        self._stop.set()
        self._refresh.set()
        if self._unsubscribe:
            self._unsubscribe()

    def _on_event(self, event):
        if event.kind == "run_done":
            self._refresh.set()

    # ----- on the main thread
    def _item(self, title, action=None, enabled=True):
        item = self.appkit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            title, action, "")
        item.setTarget_(self.delegate)
        item.setEnabled_(enabled)
        return item

    def _build(self):
        appkit = self.appkit
        self.item = appkit.NSStatusBar.systemStatusBar().statusItemWithLength_(
            appkit.NSVariableStatusItemLength)
        self.item.button().setImage_(_icon(appkit))
        self.item.button().setToolTip_("Cairn")
        menu = appkit.NSMenu.alloc().init()
        menu.setAutoenablesItems_(False)
        self.unseen_item = self._item(unseen_label(0), enabled=False)
        self.login_item = self._item("Start at login", "toggleLogin:")
        self.update_item = self._item("", "openUpdate:")
        self.update_item.setHidden_(True)
        for item in (self._item("Open Cairn", "open:"),
                     self._item("Run now", "runNow:"),
                     self.unseen_item,
                     appkit.NSMenuItem.separatorItem(),
                     self.login_item,
                     self._item("Check for updates…", "checkUpdates:"),
                     self.update_item,
                     self._item("Quit", "quit:")):
            menu.addItem_(item)
        self.item.setMenu_(menu)

    def _show_unseen(self):
        count = self.unseen.count()
        self.unseen_item.setTitle_(unseen_label(count))
        self.appkit.NSApp.dockTile().setBadgeLabel_(badge(count))

    def _latest_run(self, run):
        if self.appkit.NSApp.isActive():
            self.unseen.looked(_now())
        else:
            self.unseen.add(run)
        self._show_unseen()

    def _show_update(self, found, asked=False):
        """Show a newer release; show any answer to a check asked for from the menu."""
        self.release = found
        newer = bool(found and found["newer"])
        self.update_item.setTitle_(update_label(found))
        self.update_item.setEnabled_(newer)
        self.update_item.setHidden_(not (newer or asked))

    def _show_login(self, status):
        on = self.appkit.NSControlStateValueOn
        off = self.appkit.NSControlStateValueOff
        self.login_item.setState_(on if status["installed"] else off)

    def looked(self):
        self.unseen.looked(_now())
        if self.unseen_item is not None:
            self._show_unseen()

    def open(self):
        self.window.show()
        self.appkit.NSApp.activateIgnoringOtherApps_(True)

    def quit(self):
        self.quitting = True
        self.window.destroy()

    def toggle_login(self):
        installed = self.login_item.state() == self.appkit.NSControlStateValueOn
        action = schedule.autostart_remove if installed else schedule.autostart_install
        self._in_background(action, self._show_login)

    def check_updates(self):
        self._in_background(lambda: update.check(force=True),
                            lambda found: self._show_update(found, asked=True))

    def open_update(self):
        if self.release:
            webbrowser.open(self.release["url"])

    def run_now(self):
        threading.Thread(target=self._start_run, daemon=True).start()

    # ----- on other threads
    def _in_background(self, work, then):
        """Run work() on a thread and then(result) on the main thread; a failure is
        reported as a warn event."""
        def body():
            try:
                result = work()
            except Exception as e:  # noqa: BLE001 - the menu stays usable whatever fails
                events.emit("warn", text=f"menu bar: {type(e).__name__}: {e}")
                return
            self.call_after(then, result)
        threading.Thread(target=body, daemon=True).start()

    def _start_run(self):
        try:
            self.local.post("/api/run")
        except (OSError, ValueError) as e:
            # the window shows the warn event as a toast
            events.emit("warn", text=f"could not start a run: {_error_text(e)}")
            self.call_after(self.open)

    def _poll(self):
        while not self._stop.is_set():
            try:
                run = self.local.get("/api/status").get("latest_run")
            except (OSError, ValueError):
                run = None  # the server is stopping or busy; the next poll tries again
            if run:
                self.call_after(self._latest_run, run)
            self._refresh.wait(POLL_SECONDS)
            self._refresh.clear()


def _icon(appkit):
    """The Cairn mark, 1 pt to a grid cell, as a template image, which macOS tints to
    match the menu bar. Stones and sun are one colour; the empty cells between them
    keep the shapes apart."""
    def draw(rect):
        appkit.NSColor.blackColor().setFill()
        for cell in (mark.STONE, mark.SUN):
            for x, y, width in mark.runs(cell):
                appkit.NSBezierPath.fillRect_(((ICON_MARGIN + x, ICON_MARGIN + y), (width, 1)))
        return True

    image = appkit.NSImage.imageWithSize_flipped_drawingHandler_((18, 18), True, draw)
    image.setTemplate_(True)
    return image


def _browser_view():
    from webview.platforms.cocoa import BrowserView  # noqa: PLC0415 - see _controller
    return BrowserView


def attach(url, window):
    """The MenuBar for window, or None when AppKit cannot be loaded here or pywebview
    lacks the hooks the item needs."""
    try:
        if not all(hasattr(_browser_view(), hook) for hook in PYWEBVIEW_HOOKS):
            events.emit("warn", text="menu bar unavailable with this pywebview version")
            return None
        return MenuBar(url, window)
    except Exception as e:  # noqa: BLE001 - the window works without its menu-bar item
        events.emit("warn", text=f"no menu-bar item: {type(e).__name__}: {e}")
        return None
