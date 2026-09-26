"""Tell you the daily list is ready, without needing an account or a secret.

Anything POSTed to an ntfy.sh topic arrives as a push on every phone subscribed to
it. Anyone who knows the topic string can read it, so use a long random one.
Messages name postings and scores and never carry your profile or contact details.

A failed notification never fails a run. The ranked postings are already stored
by the time this is called, and the app still shows them.
"""
import json
import re
import subprocess
import sys
import urllib.error
import urllib.request

from cairn import tracking
from cairn.core import events, settings

TIMEOUT = 15


def _header_safe(value):
    """value when it is latin-1 with no CR or LF, else None. A CR or LF would let it
    inject a second header."""
    if not value or re.search(r"[\r\n]", value):
        return None
    try:
        value.encode("latin-1")
    except UnicodeEncodeError:
        return None
    return value


def _publish(topic, title, message, headers=()):
    req = urllib.request.Request(f"https://ntfy.sh/{topic}", data=message.encode("utf-8"),
                                 headers={"Title": title,
                                          "Content-Type": "text/plain; charset=utf-8",
                                          **dict(headers)})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return 200 <= r.status < 300


def _ntfy(title, message, link=None, priority=None, tags=None, actions=None):
    topic = (settings.get().notify_ntfy_topic or "").strip()
    if not topic:
        return False
    headers = {}
    link = _header_safe(link)
    if link:
        headers["Click"] = link
    if priority:
        headers["Priority"] = str(priority)
    if tags:
        headers["Tags"] = ",".join(tags)
    if actions:
        headers["Actions"] = "; ".join(actions)
    return _publish(topic, title, message, headers)


def test(topic):
    """Send a sample alert to topic, whether or not it is saved. Raises OSError when
    ntfy.sh cannot be reached or refuses it."""
    return _publish(topic, "Cairn can reach this phone",
                    "Alerts about new jobs will look like this.")


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
    Commas and semicolons delimit the header and would cut such a url short."""
    url = _header_safe(r.get("url"))
    if not url or re.search(r"[,;]", url):
        return None
    label = re.sub(r"[,;]", " ", str(r.get("company_name") or "role"))
    label = label.encode("latin-1", "ignore").decode("latin-1")[:18].strip() or "role"
    return f"view, Open {label}, {url}"


def run_finished(results, follow_ups=None):
    """Push a run's strong matches and due follow-ups. Returns the channels that
    accepted it.

    follow_ups holds tracking.stale_applications() rows, looked up when None. The
    buttons open the postings themselves, since the app lives on the Mac and a link
    into it is useless on a phone.
    """
    cfg = settings.get()
    if not (cfg.notify_ntfy_topic or cfg.notify_macos):
        return []

    strong = _strong(results)
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


def _strong(results):
    threshold = settings.get().fit_threshold
    return [r for r in results
            if not r.get("below_floor") and (r.get("fit") or 0) >= threshold]


def check_finished(results):
    """Push the strong matches an hourly watchlist check ranked, and nothing when it
    found none. Returns the channels that accepted it."""
    strong = _strong(results)
    if not strong:
        return []
    if len(strong) == 1:
        r = strong[0]
        title = f"New at {r.get('company_name')}"
        body = f"{r.get('title')}\nfit {r.get('fit')} · tier {r.get('tier')}"
    else:
        title = f"{len(strong)} new roles at companies you follow"
        body = "\n".join(_line(r) for r in strong[:TOP_N])
        if len(strong) > TOP_N:
            body += f"\n\n+{len(strong) - TOP_N} more"
    actions = [a for a in (_action(r) for r in strong[:ACTION_N]) if a]
    return send(title, body, link=strong[0].get("url"), priority="high",
                tags=["briefcase"], actions=actions)
