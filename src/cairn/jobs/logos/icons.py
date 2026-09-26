"""A website's icon, downloaded once and stored under paths.home()/logos."""
import collections
import os
import re
import tempfile
import time
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin

from cairn import store
from cairn.core import net, paths
from cairn.jobs.logos.web import (
    ITEM_SECONDS,
    NoIcon,
    _get,
    _may_pass,
    _reason,
    _transient,
)

MAX_BYTES = 512 * 1024

# an icon this many pixels wide or narrower is kept only when no wider one is found
SMALL_ICON = 16

_EXTENSIONS = {
    "image/x-icon": "ico", "image/vnd.microsoft.icon": "ico", "image/png": "png",
    "image/jpeg": "jpg", "image/gif": "gif", "image/svg+xml": "svg", "image/webp": "webp",
}
_CONTENT_TYPES = {"ico": "image/x-icon", "png": "image/png", "jpg": "image/jpeg",
                  "gif": "image/gif", "svg": "image/svg+xml", "webp": "image/webp"}


def path_for(domain, extension):
    return paths.home() / "logos" / f"{domain}.{extension}"


def content_type(path):
    return _CONTENT_TYPES[Path(path).suffix.lstrip(".")]


def logo_file(domain):
    """The stored icon for a domain, or None. Makes no request."""
    row = store.logo(domain)
    if row is None or row["path"] is None:
        return None
    # the store keeps the file name alone, so a home that moved still finds its icons
    path = paths.home() / "logos" / row["path"]
    return path if path.is_file() else None


# what a failed request raises: a refusal, a failed request, a malformed reply
_FAILURES = (net.Failure, ValueError)


def _image(url, deadline):
    """(bytes, extension) of an icon URL; ValueError when it is not a usable image."""
    body, kind, _ = _get(url, MAX_BYTES, deadline)
    if kind not in _EXTENSIONS:
        raise ValueError(f"{kind} is not an image")
    if len(body) > MAX_BYTES:
        raise ValueError(f"over {MAX_BYTES // 1024} KiB")
    if not body:
        raise ValueError("empty")
    return body, _EXTENSIONS[kind]


_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_ICO_HEADER = b"\0\0\1\0"


def _width(body):
    """An icon's width in pixels, the widest image's for an ICO, read from a PNG or
    ICO header; None for any other format."""
    if body.startswith(_PNG_SIGNATURE) and len(body) >= 24:
        # the IHDR chunk comes first, and its data opens with the big-endian width
        return int.from_bytes(body[16:20], "big")
    if body.startswith(_ICO_HEADER) and len(body) >= 6:
        # bytes 4-5 count the images, a 16-byte directory entry per image follows the
        # 6-byte header, and each entry opens with its image's width, 0 for 256
        entries = range(6, min(len(body), 6 + 16 * int.from_bytes(body[4:6], "little")), 16)
        return max((body[offset] or 256 for offset in entries), default=None)
    return None


class _IconLinks(HTMLParser):
    """The href and largest declared size of each <link rel="icon"> and the like."""

    RELS = frozenset({"icon", "apple-touch-icon"})

    def __init__(self):
        super().__init__()
        self.found = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        rels = set((attrs.get("rel") or "").lower().split())
        if tag != "link" or not rels & self.RELS or not attrs.get("href"):
            return
        sizes = re.findall(r"(\d+)x\d+", (attrs.get("sizes") or "").lower())
        self.found.append((max(map(int, sizes), default=0), attrs["href"]))


def _icon_links(page, base_url):
    """Absolute icon URLs declared by a page, largest first."""
    parser = _IconLinks()
    parser.feed(page.decode("utf-8", "replace"))
    ranked = sorted(parser.found, key=lambda found: -found[0])
    return [urljoin(base_url, href) for _, href in ranked]


_DUCKDUCKGO = "https://icons.duckduckgo.com/ip3/{domain}.ico"
_GSTATIC = ("https://t0.gstatic.com/faviconV2?client=SOCIAL&type=FAVICON"
            "&fallback_opts=TYPE,SIZE,URL&url=https://{domain}&size=128")


def _icon_urls(domain, deadline, errors):
    """(source, url) of each icon to try for a domain, in order: /favicon.ico, the
    icons the home page links to, then DuckDuckGo's and Google's favicon services,
    which answer 404 for a domain they have no icon for. The home page is requested
    only once /favicon.ico has been tried, and its failure is added to errors as
    (reason, whether it may pass)."""
    yield "site", f"https://{domain}/favicon.ico"
    home = f"https://{domain}/"
    try:
        page, _, final_url = _get(home, MAX_BYTES, deadline)
    except _FAILURES as e:
        errors.append((f"{home}: {_reason(e)}", _transient(e)))
    else:
        for url in _icon_links(page[:MAX_BYTES], final_url):
            yield "site", url
    yield "duckduckgo", _DUCKDUCKGO.format(domain=domain)
    yield "gstatic", _GSTATIC.format(domain=domain)


def _find_icon(domain, deadline):
    """(bytes, extension, source) of the first icon found wider than SMALL_ICON
    pixels, else of the first icon found; NoIcon when there is none."""
    errors, small = [], None
    for source, url in _icon_urls(domain, deadline, errors):
        try:
            body, extension = _image(url, deadline)
        except _FAILURES as e:
            errors.append((f"{url}: {_reason(e)}", _transient(e)))
            continue
        width = _width(body)
        if width is None or width > SMALL_ICON:
            return body, extension, source
        small = small or (body, extension, source)
    if small:
        return small
    raise NoIcon(f"no usable icon: {'; '.join(reason for reason, _ in errors)}",
                 _may_pass([transient for _, transient in errors], deadline))


def _save(domain, body, extension):
    path = path_for(domain, extension)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("wb", dir=path.parent, prefix=f".{path.name}.",
                                     delete=False) as f:
        f.write(body)
    os.replace(f.name, path)
    for other in _CONTENT_TYPES:
        if other != extension:
            path_for(domain, other).unlink(missing_ok=True)
    return path


# The outcome of _icon for a domain. path and source name the saved icon and what
# served it, and error and transient say why none was saved and whether that may pass
# on a later try.
Download = collections.namedtuple("Download", "path error source transient")


def _icon(domain, deadline):
    """The Download of a domain's icon."""
    try:
        body, extension, source = _find_icon(domain, deadline)
        return Download(_save(domain, body, extension), None, source, False)
    except Exception as e:  # noqa: BLE001 - any failure leaves the monogram in place
        return Download(None, _reason(e), None, _transient(e))


def _record(domain, download):
    """Store a download's outcome. A failure that may pass leaves no row, so the
    next pass tries the domain again."""
    if not download.transient:
        store.save_logo(domain, download.path, download.error, download.source)


def fetch(domain, deadline=None):
    """Download and store a domain's icon, recording the outcome. The path, or None.
    No request outlives `deadline`, by default ITEM_SECONDS from now."""
    download = _icon(domain, deadline or time.monotonic() + ITEM_SECONDS)
    _record(domain, download)
    return download.path
