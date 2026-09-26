"""Tell you the daily list is ready, without needing an account or a secret.

ntfy.sh is a free pub/sub relay: you pick an unguessable topic name, subscribe the
phone app to it, and anything POSTed to that topic arrives as a push. No signup, no
API key, nothing to rotate — which is why it is the default here over SMTP.

A topic is a shared secret only in the sense that anyone who knows the string can
read it, so use a long random one. Nothing sensitive goes in the message: company
names and fit scores, never your profile or contact details.

Notification failures never fail a run. The ranked postings are already stored by the
time this is called, and the app still shows them.
"""
import json
import re
import subprocess
import sys
import urllib.error
import urllib.request

from cairn import events, settings, tracking

TIMEOUT = 15


def _header_safe(value):
    """value usable as one line of an HTTP header: latin-1, and free of the CR or LF
    that would let it inject a second header or start a new one."""
    if not value or re.search(r"[\r\n]", value):
        return None
    try:
        value.encode("latin-1")
    except UnicodeEncodeError:
        return None
    return value


def _ntfy(title, message, link=None, priority=None, tags=None, actions=None):
    topic = (settings.get().notify_ntfy_topic or "").strip()
    if not topic:
        return False
    headers = {"Title": title, "Content-Type": "text/plain; charset=utf-8"}
    link = _header_safe(link)
    if link:
        headers["Click"] = link
    if priority:
        headers["Priority"] = str(priority)
    if tags:
        headers["Tags"] = ",".join(tags)
    if actions:
        headers["Actions"] = "; ".join(actions)
    req = urllib.request.Request(f"https://ntfy.sh/{topic}",
                                 data=message.encode("utf-8"), headers=headers)
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return 200 <= r.status < 300


def _macos_banner(title, message):
    if sys.platform != "darwin" or not settings.get().notify_macos:
        return False
    # json.dumps does the AppleScript string quoting for us, including embedded
    # quotes in a company name.
    script = (f"display notification {json.dumps(message)} "
              f"with title {json.dumps(title)}")
    subprocess.run(["osascript", "-e", script], capture_output=True, timeout=TIMEOUT)
    return True


def send(title, message, link=None, priority=None, tags=None, actions=None):
    """Best-effort push. Returns the channels that accepted it."""
    delivered = []
    for name, fn in (("ntfy", lambda: _ntfy(title, message, link, priority, tags, actions)),
                     ("macos", lambda: _macos_banner(title, message))):
        try:
            if fn():
                delivered.append(name)
        except (urllib.error.URLError, urllib.error.HTTPError, OSError,
                subprocess.SubprocessError, TimeoutError, ValueError) as e:
            events.emit("warn", text=f"[notify] {name} failed: {type(e).__name__}: "
                                     f"{str(e)[:60]}")
    return delivered


TOP_N = 3          # leads listed in the body
ACTION_N = 2       # ntfy renders at most three buttons; two keeps them wide enough
EXCEPTIONAL = 90   # fit AND tier at or above this is worth buzzing through for


def _line(r):
    """'NVIDIA — Systems Software Eng   95/92', trimmed for a lock screen."""
    role = f"{r.get('company_name')} — {r.get('title')}"
    if len(role) > 38:
        role = role[:37].rstrip() + "…"
    return f"{role:<38} {r.get('fit')}/{r.get('tier')}"


def _action(r):
    """An ntfy view button, or None for a url the Actions header cannot carry.
    Commas and semicolons delimit the header, so a url with either is dropped
    rather than mangled into a link that no longer opens the posting."""
    url = _header_safe(r.get("url"))
    if not url or re.search(r"[,;]", url):
        return None
    label = re.sub(r"[,;]", " ", str(r.get("company_name") or "role"))
    label = label.encode("latin-1", "ignore").decode("latin-1")[:18].strip() or "role"
    return f"view, Open {label}, {url}"


def run_finished(results, follow_ups=None):
    """Summarise a run: what cleared the bar, the applications that need a
    follow-up, and a way to act on it from the phone.

    follow_ups holds tracking.stale_applications() rows, looked up when None. The
    app lives on the Mac, so a link into it is useless on a phone. The buttons point
    at the postings themselves, which open anywhere.
    """
    cfg = settings.get()
    if not (cfg.notify_ntfy_topic or cfg.notify_macos):
        return []

    strong = [r for r in results
              if not r.get("below_floor") and (r.get("fit") or 0) >= cfg.fit_threshold]
    top = strong[:TOP_N]

    if not results:
        title, priority = "No new postings today", "low"
    elif not strong:
        title, priority = f"{len(results)} new, no strong matches", "low"
    else:
        exceptional = any((r.get("fit") or 0) >= EXCEPTIONAL
                          and (r.get("tier") or 0) >= EXCEPTIONAL for r in strong)
        title = f"{len(strong)} strong match{'es' if len(strong) != 1 else ''} today"
        priority = "high" if exceptional else "default"

    body = "\n".join(_line(r) for r in top)
    tail = []
    if len(strong) > len(top):
        tail.append(f"+{len(strong) - len(top)} more strong")
    others = len(results) - len(strong)
    if others > 0:
        tail.append(f"{others} other{'s' if others != 1 else ''} ranked")
    if tail:
        body += ("\n\n" if body else "") + " · ".join(tail)
    if not body:
        body = "The run ranked nothing new."
    if cfg.notify_follow_ups:
        reminder = tracking.follow_up_note(
            tracking.stale_applications() if follow_ups is None else follow_ups)
        if reminder:
            body += "\n\n" + reminder

    actions = [a for a in (_action(r) for r in top[:ACTION_N]) if a]
    return send(title, body, link=(top[0].get("url") if top else None),
                priority=priority, tags=["briefcase"], actions=actions)
