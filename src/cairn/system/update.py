"""Whether a newer release is out on GitHub, asked at most once a day."""
import datetime
import json
import re
import urllib.request
from importlib import metadata
from urllib.parse import urlparse

from cairn import __version__
from cairn.core import events, paths

TIMEOUT = 5  # seconds for the GitHub request
INTERVAL = datetime.timedelta(hours=24)
RETRY_INTERVAL = datetime.timedelta(hours=1)  # after a failed request
PLACEHOLDER = "<owner>"
# PEP 440 and semver spellings alike: 1.2.3, v1.2, 1.0.0rc1, 1.0.0-rc.1, 2.0.0b2
VERSION = re.compile(r"v?(\d+(?:\.\d+)*)(?:[-.]?(dev|a|alpha|b|beta|c|rc|pre|preview)"
                     r"[-.]?(\d*))?", re.IGNORECASE)
PRE_RANK = {"dev": 0, "a": 1, "alpha": 1, "b": 2, "beta": 2, "c": 3, "rc": 3, "pre": 3,
            "preview": 3}
FINAL = (4, 0)  # a release sorts after every pre-release of the same number

_placeholder_warned = False


def cache_file():
    return paths.home() / "update.json"


def repository():
    """The Repository URL from the package metadata, or None."""
    try:
        urls = metadata.metadata("cairn-jobs").get_all("Project-URL") or []
    except metadata.PackageNotFoundError:
        return None
    for entry in urls:
        label, _, url = entry.partition(",")
        if label.strip() == "Repository":
            return url.strip()
    return None


def _key(version):
    """A sort key for a version string, or None when it is not one.

    Trailing zeros are dropped so 1.2 equals 1.2.0, and a pre-release sorts before
    the release it precedes.
    """
    match = VERSION.fullmatch(version.strip())
    if match is None:
        return None
    release, tag, number = match.groups()
    parts = [int(n) for n in release.split(".")]
    while len(parts) > 1 and parts[-1] == 0:
        parts.pop()
    pre = (PRE_RANK[tag.lower()], int(number or 0)) if tag else FINAL
    return (tuple(parts), pre)


def is_newer(latest, current):
    latest_key, current_key = _key(latest), _key(current)
    return latest_key is not None and current_key is not None and latest_key > current_key


def _cached(now):
    try:
        cached = json.loads(cache_file().read_text(encoding="utf-8"))
        checked_at = datetime.datetime.fromisoformat(cached["checked_at"])
        interval = INTERVAL if cached["release"] else RETRY_INTERVAL
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if now - checked_at >= interval:
        return None
    return cached


def _release_page(url):
    parts = urlparse(url) if isinstance(url, str) else None
    return url if parts and parts.scheme == "https" and parts.hostname == "github.com" else None


def _latest(repo_url):
    owner_repo = urlparse(repo_url).path.strip("/")
    request = urllib.request.Request(
        f"https://api.github.com/repos/{owner_repo}/releases/latest",
        headers={"Accept": "application/vnd.github+json",
                 "User-Agent": f"cairn/{__version__}"})
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
        release = json.load(response)
    return {"latest": release["tag_name"].removeprefix("v"),
            "url": _release_page(release.get("html_url")),
            "published_at": release.get("published_at")}


def _result(release):
    return {"current": __version__, **release,
            "newer": is_newer(release["latest"], __version__)}


def _check(force):
    global _placeholder_warned
    repo_url = repository()
    if not repo_url or PLACEHOLDER in repo_url:
        if not _placeholder_warned:
            _placeholder_warned = True
            events.emit("warn", text="update check skipped: the package names no "
                                     "GitHub repository")
        return None
    now = datetime.datetime.now(datetime.UTC)
    cached = None if force else _cached(now)
    if cached is not None:
        return cached["release"] and _result(cached["release"])
    try:
        release = _latest(repo_url)
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as e:
        events.emit("warn", text=f"update check failed: {type(e).__name__}: {e}")
        release = None
    cache_file().parent.mkdir(parents=True, exist_ok=True)
    cache_file().write_text(json.dumps({"checked_at": now.isoformat(), "release": release}),
                            encoding="utf-8")
    return release and _result(release)


def check(force=False):
    """{current, latest, url, published_at, newer}, or None when unknown. Never raises.

    url is None unless GitHub named an https page on github.com. The answer is cached
    in update.json, for a day, or for an hour after a failed request; force asks
    GitHub regardless.
    """
    try:
        return _check(force)
    except Exception as e:  # noqa: BLE001 - callers run it on a thread or at startup
        events.emit("warn", text=f"update check failed: {type(e).__name__}: {e}")
        return None
