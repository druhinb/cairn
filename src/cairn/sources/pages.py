"""Plain careers pages, read for links that name a role."""
import collections
import html
import html.parser
import re
import time
from urllib.parse import parse_qsl, urldefrag, urlencode, urljoin, urlparse

from cairn.core import events
from cairn.sources import web
from cairn.sources.common import _posting

PAGE_TIMEOUT = 20
PAGE_MAX_BYTES = 512 * 1024
PAGE_MAX_LINKS = 300
PAGE_MAX_TAGS = 50_000
# an anchor's parent and up to this many more ancestors are searched for its location
PAGE_CONTEXT_LEVELS = 3
# the text runs read on each side of an anchor for its location, and inside it for
# its title and places
PAGE_CONTEXT_RUNS = 25
# query parameters that name the job a careers page link points to
_PAGE_ID_PARAMS = frozenset({"id", "jobid", "job_id", "gh_jid", "req", "requisition",
                             "posting"})
_PAGE_ROLE = re.compile(r"\b(engineer|developer|scientist|analyst|researcher|intern|"
                        r"graduate|associate|sde|swe|quant)", re.IGNORECASE)
# "San Francisco, CA", "London, United Kingdom", "Austin, TX, United States"
_PAGE_PLACE = re.compile(r"[A-Z][\w.'-]*(?: [A-Z][\w.'-]*)*"
                         r"(?:, [A-Z][\w.'-]*(?: [A-Z][\w.'-]*)*){1,2}")
_REMOTE_WORD = re.compile(r"\bremote\b", re.IGNORECASE)
_VOID_TAGS = frozenset({"area", "base", "br", "col", "embed", "hr", "img", "input", "link",
                        "meta", "source", "track", "wbr"})
# how many start tags pass between two looks at the clock
_CLOCK_EVERY = 256


_PLACE_SEPARATOR = re.compile(r"\s+[|/·•]\s+|;\s*")


def _run_places(run):
    """The places a run of text lists, such as "Austin, TX | Remote", or [] when any
    part of it is not a place. Matching whole runs keeps a title such as "Manager,
    AI Platform" from reading as a city and state."""
    parts = _PLACE_SEPARATOR.split(run)
    if all(_PAGE_PLACE.fullmatch(part) or (_REMOTE_WORD.search(part) and len(part) <= 40)
           for part in parts):
        return parts
    return []


def _names_role(title):
    return 8 <= len(title) <= 120 and bool(_PAGE_ROLE.search(title))


class _Element:
    __slots__ = ("tag", "href", "start", "unplaced", "anchor_count")

    def __init__(self, tag, href, start):
        # start indexes the element's first text run in _Anchors.runs
        self.tag, self.href, self.start = tag, href, start
        # anchors below this element still without a location, and all anchors below it
        self.unplaced, self.anchor_count = [], 0


class _Cut(Exception):
    """Parsing stops early."""


def _web_link(base_url, href):
    href = href.strip()
    if not href or href.startswith("#"):
        return None
    link = urldefrag(urljoin(base_url, href))[0]
    return link if urlparse(link).scheme in ("http", "https") else None


class _Anchors(html.parser.HTMLParser):
    """The web link, title, and places of up to PAGE_MAX_LINKS anchors on the page at
    base_url whose title names a role. A card whose anchor wraps a title run and then
    place runs is split between them; any other anchor takes the places named in the
    nearest enclosing element that holds no other anchor.

    Every text run is kept once, in document order, and an element records where
    its runs start, so the work grows with the page. Parsing stops at PAGE_MAX_TAGS
    start tags or at the deadline, and cut_short says why.
    """

    def __init__(self, base_url, deadline):
        super().__init__(convert_charrefs=True)
        self.anchors, self.runs, self.cut_short = [], [], None
        self._base_url, self._deadline = base_url, deadline
        self._open = [_Element("", None, 0)]
        self._open_tags = collections.Counter()
        self._tags = 0

    def parse(self, text):
        try:
            self.feed(text)
            self.close()
        except _Cut as e:
            self.cut_short = str(e)
        while len(self._open) > 1:
            self._close()

    def handle_starttag(self, tag, attrs):
        self._tags += 1
        if self._tags > PAGE_MAX_TAGS:
            raise _Cut(f"over {PAGE_MAX_TAGS} tags")
        if self._tags % _CLOCK_EVERY == 0 and time.monotonic() > self._deadline:
            raise _Cut("out of time")
        if tag not in _VOID_TAGS:
            href = dict(attrs).get("href") if tag == "a" else None
            self._open.append(_Element(tag, href, len(self.runs)))
            self._open_tags[tag] += 1

    def handle_endtag(self, tag):
        # an end tag closes every element left open inside it
        if self._open_tags[tag]:
            while self._close() != tag:
                pass

    def handle_data(self, data):
        run = " ".join(data.split())
        if run and self._open[-1].tag not in ("script", "style"):
            self.runs.append(run)

    def _close(self):
        element = self._open.pop()
        self._open_tags[element.tag] -= 1
        parent = self._open[-1]
        parent.anchor_count += element.anchor_count
        if element.tag == "a":
            if element.href:
                self._add_anchor(element, parent)
            return element.tag
        if element.anchor_count == 1 and element.unplaced:
            (anchor,) = element.unplaced
            places = [place for run in self._around(element, anchor)
                      for place in _run_places(run)]
            if places:
                anchor["places"] = list(dict.fromkeys(places))
            elif anchor["levels"] < PAGE_CONTEXT_LEVELS:
                anchor["levels"] += 1
                parent.unplaced.append(anchor)
        return element.tag

    def _around(self, element, anchor):
        """The runs of element nearest its anchor, outside the anchor."""
        start, end = anchor["runs"]
        return (self.runs[max(element.start, start - PAGE_CONTEXT_RUNS):start]
                + self.runs[end:end + PAGE_CONTEXT_RUNS])

    def _add_anchor(self, element, parent):
        parent.anchor_count += 1
        end = len(self.runs)
        runs = self.runs[element.start:min(end, element.start + PAGE_CONTEXT_RUNS)]
        split = next((i for i, run in enumerate(runs) if i and _run_places(run)), len(runs))
        title = " ".join(runs[:split])
        link = _web_link(self._base_url, element.href)
        if len(self.anchors) == PAGE_MAX_LINKS or not link or not _names_role(title):
            return
        places = [place for run in runs[split:] for place in _run_places(run)]
        anchor = {"link": link, "title": title, "runs": (element.start, end),
                  "places": list(dict.fromkeys(places)), "levels": 0}
        self.anchors.append(anchor)
        if not places:
            parent.unplaced.append(anchor)


def _page_id(link):
    """page: and the link lowercased, less its trailing slash and every query
    parameter but those in _PAGE_ID_PARAMS."""
    base, _, query = link.partition("?")
    kept = [(name, value) for name, value in parse_qsl(query)
            if name.casefold() in _PAGE_ID_PARAMS]
    path = base.lower().rstrip("/")
    return f"page:{path}?{urlencode(kept)}" if kept else f"page:{path}"


def page(url, company):
    """The links on a careers page whose text names a role; each one is a posting.
    The first PAGE_MAX_BYTES of the page are read."""
    deadline = time.monotonic() + PAGE_TIMEOUT
    parser = _Anchors(url, deadline)
    parser.parse(web.read_text(url, deadline, PAGE_MAX_BYTES, cut=True))
    if parser.cut_short:
        events.emit("warn", text=f"[fetch] page:{url}: read part of the page, "
                                 f"{parser.cut_short}")
    rows = {}
    for anchor in parser.anchors:
        posting_id = _page_id(anchor["link"])
        rows.setdefault(posting_id, _posting(posting_id, company, anchor["title"],
                                             anchor["link"], anchor["places"], None, None))
    return list(rows.values())
