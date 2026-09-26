"""Company icons, fetched once per domain and served from paths.home()/logos.

The browser only ever asks the local server for an icon; the server finds each
company's website and downloads its favicon during a run, `cairn icons`, or
in the background soon after a page shows the company with none, never on a request
for the icon. A website comes from the feed's company_url, else from an
apply URL on the company's own domain, else from Clearbit's autocomplete or
Wikidata under the company's exact name, else from a domain guessed from the name
that the site's own title confirms. A company with none of these shows the UI's
monogram. The icon comes from the site itself, else from DuckDuckGo's or Google's
favicon service, and a website none of them has an icon for gives way to the one the
next method finds.

A failure that may pass on a later try, from the network, a timeout, or a server
answering 429 or 5xx, records nothing, so the company or icon stays pending.
"""
import collections
import contextlib
import functools
import http.client
import json
import os
import re
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, wait
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlencode, urljoin, urlsplit

from cairn import store
from cairn.core import events, locks, net, paths

MAX_BYTES = 512 * 1024
# the part of a home page searched for its title
PAGE_BYTES = 64 * 1024
JSON_BYTES = 64 * 1024
# wall-clock limits on one request, from its start
HEADER_SECONDS = 5
REQUEST_SECONDS = 10
# every request made for one company or one icon, redirects and fallbacks included
ITEM_SECONDS = 15
RETRY_AFTER = store.ICON_RETRY_DAYS * 86400
# 400 lookups by 12 workers took 72 s of a 90 s budget on 17k postings
MAX_PER_RUN = 400
BUDGET_SECONDS = 90
# the part of a pass's budget after which no new website lookup starts, which leaves
# the rest to icon downloads
RESOLVE_SHARE = 0.6
WORKERS = 12
# an icon this many pixels wide or narrower is kept only when no wider one is found
SMALL_ICON = 16
# a pass whose requests, this many or more, all failed in a way that may pass finds the
# network down
OFFLINE_ATTEMPTS = 5
OFFLINE = "network unavailable"
# companies queued in a Wanted, the seconds before one is queued again, and the
# budget of the pass that fetches them
WANTED_MAX = 100
WANTED_AGAIN = 600
WANTED_BUDGET = 30

_EXTENSIONS = {
    "image/x-icon": "ico", "image/vnd.microsoft.icon": "ico", "image/png": "png",
    "image/jpeg": "jpg", "image/gif": "gif", "image/svg+xml": "svg", "image/webp": "webp",
}
_CONTENT_TYPES = {"ico": "image/x-icon", "png": "image/png", "jpg": "image/jpeg",
                  "gif": "image/gif", "svg": "image/svg+xml", "webp": "image/webp"}

# Job boards, applicant trackers, and listing sites, by registrable domain. Their
# icon would be the same on every company's row. Simplify's feeds point
# company_url at simplify.jobs/c/<name>.
AGGREGATORS = frozenset({
    "adp.com", "airtable.com", "angel.co", "applicantpro.com", "applytojob.com",
    "ashbyhq.com", "avature.net", "bamboohr.com", "brassring.com", "breezy.hr",
    "careerpuck.com", "dayforcehcm.com", "dover.com", "eightfold.ai", "forms.gle",
    "gem.com", "github.com", "glassdoor.com", "gr8people.com", "greenhouse.io",
    "grnh.se", "hirebridge.com", "hiringthing.com", "hrmdirect.com", "icims.com",
    "indeed.com", "jibeapply.com", "jobs2web.com", "jobvite.com", "lever.co",
    "linkedin.com", "myworkday.com", "myworkdayjobs.com", "myworkdaysite.com",
    "notion.site", "oraclecloud.com", "paylocity.com", "personio.de", "phenom.com",
    "pinpointhq.com", "recruitee.com", "recsolu.com", "ripplematch.com", "rippling.com",
    "sapsf.com", "simplify.jobs", "smartrecruiters.com", "successfactors.com",
    "tally.so", "taleo.net", "talroo.com", "teamtailor.com", "typeform.com",
    "ultipro.com", "wellfound.com", "workable.com", "workatastartup.com",
    "ycombinator.com", "yello.co",
})
# paths under which a company's own domain hosts other people's pages
_AGGREGATOR_PATHS = {"google.com": ("/forms",)}
# RFC 2606 reserves these for examples and tests
_RESERVED = frozenset({"example.com", "example.net", "example.org"})
# second-level labels a country's registry hands out names under, as the co of
# standardbank.co.za
_GENERIC_LABELS = frozenset({"ac", "co", "com", "edu", "gob", "gov", "ltd", "mil", "ne",
                             "net", "nhs", "or", "org", "plc", "sch"})
# hosts that give each customer a subdomain, so their registrable domain names no company
_HOSTING = frozenset({
    "blogspot.com", "carrd.co", "github.io", "godaddysites.com", "herokuapp.com",
    "myshopify.com", "netlify.app", "notion.site", "pages.dev", "squarespace.com",
    "vercel.app", "webflow.io", "weebly.com", "wixsite.com", "wordpress.com",
})

def domain_for(job):
    """The website whose icon a posting shows, or None."""
    return job.get("logo_domain")


def registrable(host):
    """The domain a host's owner registered, as apple.com for jobs.apple.com and
    standardbank.co.za for www.standardbank.co.za. None for a public suffix such as
    co.za, and for a hosting service's customer such as acme.github.io."""
    labels = host.split(".")
    keep = 3 if len(labels) > 2 and labels[-2] in _GENERIC_LABELS else 2
    domain = ".".join(labels[-keep:])
    if labels[-keep] in _GENERIC_LABELS or domain in _HOSTING:
        return None
    return domain


def _company_site(url):
    """The registrable domain of url when it is a company's own site, else None."""
    url = store.web_url(url)
    if url is None:
        return None
    parts = urlsplit(url)
    host = parts.hostname or ""
    if not net.public_name(host):
        return None
    domain = registrable(host)
    if (domain is None or domain in AGGREGATORS or domain in _RESERVED
            or parts.path.startswith(_AGGREGATOR_PATHS.get(domain, ()))):
        return None
    return domain


# the legal forms a company name may end in
_LEGAL_SUFFIXES = frozenset({"inc", "llc", "ltd", "corp", "corporation", "co", "plc", "gmbh",
                             "sa", "ag", "pty"})
# trailing words a company's domain usually leaves out
_NAME_SUFFIXES = _LEGAL_SUFFIXES | {"technologies", "labs", "group", "holdings"}
_PUNCTUATION = re.compile(r"[^\w\s]")
# a domain written in a page title, as in a parked page's "acme.com is for sale"
_DOMAIN_TEXT = re.compile(r"[\w-]+(\.[\w-]+)+")
# the separators between a title's parts, as in "Envoy | Workplace platform" and
# Anthropic's "Home \ Anthropic"
_TITLE_PARTS = re.compile(r"\s[-:\\]\s|[|–—·•]")
# a last word the company's domain may carry as its top-level domain, as scale.ai does
# for Scale AI
_TLD_WORDS = {"ai": "ai", "labs": "ai", "io": "io"}
# One-word names this short are common words and acronyms, whose titles matched
# unrelated sites such as a hormone clinic at hrt.com.
SHORT_NAME = 5


def _stripped(name, suffixes):
    words = _PUNCTUATION.sub("", name.casefold()).split()
    while words and words[-1] in suffixes:
        words.pop()
    return words


def _name_words(name):
    """The words of a company name that its domain would spell, lowercased."""
    return _stripped(name, _NAME_SUFFIXES)


def _legal_words(name):
    """The words of a company name less its legal form, lowercased."""
    return _stripped(name, _LEGAL_SUFFIXES)


# a trailing part in parentheses, as the (SIG) of "Susquehanna International Group (SIG)"
_PARENTHESISED = re.compile(r"(.*?\S)\s*\(([^()]*)\)\s*")
# a parenthesised part that gives the company's initials
_INITIALS = re.compile(r"[A-Z]{2,6}")

# The lookups search for text and match a suggestion's name to words, the name less
# its legal form. A domain may spell one of the initials in place of the words.
Name = collections.namedtuple("Name", "text words initials")


def parse_name(company_name):
    """The Name of a company. A trailing parenthesised part stays out of its text,
    and counts among its initials when written as 2 to 6 capitals. A single letter
    never counts as initials."""
    text, initials = company_name, set()
    parenthesised = _PARENTHESISED.fullmatch(company_name)
    if parenthesised:
        text = parenthesised[1]
        if _INITIALS.fullmatch(parenthesised[2].strip()):
            initials.add(parenthesised[2].strip().casefold())
    words = _legal_words(text)
    initials.add("".join(word[0] for word in words))
    return Name(text, words, {found for found in initials if len(found) >= 2})


def _guesses(name):
    """Domains a company named `name` plausibly owns, most likely first. Job sites
    and reserved names are left out."""
    raw, words = _PUNCTUATION.sub("", name.casefold()).split(), _name_words(name)
    if not words:
        return []
    found = [f"{''.join(words)}.com", f"{'-'.join(words)}.com"]
    tld = _TLD_WORDS.get(raw[-1])
    if tld and len(raw) > 1:
        found.append(f"{''.join(raw[:-1])}.{tld}")
    return [domain for domain in dict.fromkeys(found)
            if _company_site(f"https://{domain}/") == domain]


def _words(text):
    """The words of text, lowercased, split at punctuation, domains in it left out."""
    return _PUNCTUATION.sub(" ", _DOMAIN_TEXT.sub(" ", text.casefold())).split()


def _spells(part, joined):
    """Whether consecutive words of part, run together, are `joined`."""
    words = _words(part)
    for start in range(len(words)):
        run = ""
        for word in words[start:]:
            run += word
            if run == joined:
                return True
            if not joined.startswith(run):
                break
    return False


def _names(titles, site_names, words):
    """Whether a page's titles and og:site_name values name the company.

    A multi-word name must appear in one title part as its words in order, spaced
    or run together. A one-word name needs a part that is the name alone, and one
    of SHORT_NAME letters or fewer needs og:site_name to be the name. Any token
    match accepted prevail.com, an incontinence brand, for Prevail the employer.
    """
    parts = [part for text in titles + site_names for part in _TITLE_PARTS.split(text)]
    if len(words) > 1:
        return any(_spells(part, "".join(words)) for part in parts)
    if len(words[0]) <= SHORT_NAME:
        return any(_name_words(site_name) == words for site_name in site_names)
    return any(_name_words(part) == words for part in parts)


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


class NoIcon(ValueError):
    """No usable icon for a domain, `transient` when every failure behind it may pass."""

    def __init__(self, message, transient):
        super().__init__(message)
        self.transient = transient


def _transient(error):
    """Whether a failure may pass on a later try, as a network or TLS error, a
    timeout, or a server answering 429 or 5xx may."""
    if isinstance(error, (NoIcon, net.Failure)):
        return error.transient
    return isinstance(error, (OSError, http.client.HTTPException))


def _may_pass(transient, deadline):
    """Whether an item may succeed on a later try, given one flag per failure saying
    whether it may pass. It may when every failure may, or when the item ran out of
    time."""
    return (bool(transient) and all(transient)) or time.monotonic() >= deadline


def _reason(error):
    return str(error) or type(error).__name__


def _get(url, limit, deadline):
    """(body, content type, final url) of a GET, each hop's headers due within
    HEADER_SECONDS and its reply within REQUEST_SECONDS, the body cut at limit + 1
    bytes."""
    reply = net.get(url, limit=limit, deadline=deadline, header_seconds=HEADER_SECONDS,
                    request_seconds=REQUEST_SECONDS)
    return reply.body, reply.content_type, reply.final_url


class _SiteName(HTMLParser):
    """A page's <title> text and og:site_name."""

    def __init__(self):
        super().__init__()
        self.titles, self.site_names, self._in_title = [], [], False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        self._in_title = tag == "title"
        if tag == "meta" and attrs.get("property") == "og:site_name" and attrs.get("content"):
            self.site_names.append(attrs["content"])

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False

    def handle_data(self, data):
        if self._in_title:
            self.titles.append(data)


def _verify(domain, words, deadline):
    """The registrable domain the site at `domain` settles on when its title or
    og:site_name names the company; ValueError otherwise."""
    page, _, final_url = _get(f"https://{domain}/", PAGE_BYTES, deadline)
    settled = _company_site(final_url)
    if settled is None:
        raise ValueError(f"redirected to {urlsplit(final_url).hostname}")
    parser = _SiteName()
    parser.feed(page[:PAGE_BYTES].decode("utf-8", "replace"))
    if not _names(parser.titles, parser.site_names, words):
        title = " ".join(" ".join(parser.titles + parser.site_names).split())
        raise ValueError(f"title {title[:80]!r} does not name the company")
    return settled


def _guess(name, deadline):
    """(domain, "guess-verified", []), or (None, why each guess was rejected, whether
    each rejection may pass on a later try)."""
    words, rejected, transient = _name_words(name), [], []
    for candidate in _guesses(name):
        try:
            return _verify(candidate, words, deadline), "guess-verified", []
        except Exception as e:  # noqa: BLE001 - an unreachable site is a rejected guess
            rejected.append(f"{candidate}: {_reason(e)}")
            transient.append(_transient(e))
    return None, "; ".join(rejected) or "no guess from the name", transient


_CLEARBIT = "https://autocomplete.clearbit.com/v1/companies/suggest?"
_WIKIDATA = "https://www.wikidata.org/w/api.php?"
# words in a Wikidata description that mark the entity as an organisation, so a
# person or a place under the company's name is passed over
_ORGANISATION_WORDS = frozenset({
    "agency", "airline", "automaker", "bank", "brand", "business", "company",
    "conglomerate", "consultancy", "corporation", "developer", "enterprise", "exchange",
    "firm", "fund", "hospital", "institute", "laboratory", "manufacturer", "organisation",
    "organization", "provider", "publisher", "retailer", "startup", "studio", "subsidiary",
    "university",
})


def _json(url, deadline):
    body, _, _ = _get(url, JSON_BYTES, deadline)
    if len(body) > JSON_BYTES:
        raise ValueError(f"reply over {JSON_BYTES // 1024} KiB")
    return json.loads(body)


def _dig(data, *keys):
    """data[key][key]..., or None where a level is missing or not a JSON object."""
    for key in keys:
        if not isinstance(data, dict):
            return None
        data = data.get(key)
    return data


def _text(data, *keys):
    """The string at data[key][key]..., or "" where there is none."""
    found = _dig(data, *keys)
    return found if isinstance(found, str) else ""


def _clearbit(name, deadline):
    """The site of Clearbit's top suggestion when that suggestion has the company's
    name, legal form aside, and a domain that spells part of the name; ValueError
    otherwise. A lower suggestion is never taken, and neither is a near miss such as
    Hrvatska radiotelevizija's hrt.hr for HRT."""
    suggestions = _json(_CLEARBIT + urlencode({"query": name.text}), deadline)
    if not isinstance(suggestions, list):
        raise ValueError("the reply is not a list of suggestions")
    if not suggestions:
        raise ValueError("no suggestions")
    top = suggestions[0]
    if not _text(top, "name") or _legal_words(_text(top, "name")) != name.words:
        raise ValueError("the top suggestion does not have the company's name")
    domain = _text(top, "domain")
    if not domain:
        raise ValueError("the top suggestion has no domain")
    site = _company_site(f"https://{domain}/")
    if site is None:
        raise ValueError(f"{domain[:80]} is not a company's own site")
    if not _spells_name(site, name):
        raise ValueError(f"{site} holds no word of the name of 3 letters or more, "
                         "nor its initials")
    return site


def _spells_name(domain, name):
    """Whether a domain's first label holds a word of the name 3 letters long or more,
    or is one of the name's initials. Clearbit gave lexingtonmenus.com for GE Aerospace."""
    label = domain.split(".")[0]
    return (label in name.initials
            or any(len(word) >= 3 and word in label for word in name.words))


def _official_sites(entity, deadline):
    """The registrable domains of a Wikidata entity's official websites (P856)."""
    found = _json(_WIKIDATA + urlencode({"action": "wbgetclaims", "entity": entity,
                                         "property": "P856", "format": "json"}), deadline)
    claims = _dig(found, "claims", "P856")
    sites = (_company_site(_dig(claim, "mainsnak", "datavalue", "value"))
             for claim in (claims if isinstance(claims, list) else []))
    return [site for site in sites if site]


def _wikidata(name, deadline):
    """The official website of the first Wikidata organisation labelled with the
    company's name, legal form aside, whose domain spells part of the name;
    ValueError when none has one."""
    found = _json(_WIKIDATA + urlencode({"action": "wbsearchentities", "search": name.text,
                                         "language": "en", "format": "json", "limit": 5}),
                  deadline)
    hits = _dig(found, "search")
    if not isinstance(hits, list):
        raise ValueError("the reply holds no search results")
    rejected = []
    for hit in hits:
        entity, label = _text(hit, "id"), _text(hit, "label")
        if (not entity or not label or _legal_words(label) != name.words
                or not _ORGANISATION_WORDS & set(_words(_text(hit, "description")))):
            continue
        sites = _official_sites(entity, deadline)
        spelled = [site for site in sites if _spells_name(site, name)]
        if spelled:
            return spelled[0]
        rejected.append(f"{entity}'s website {sites[0]} holds no word of the name" if sites
                        else f"{entity} has no official website")
    raise ValueError("; ".join(rejected) or "no organisation has the company's name")


# lookups by name, tried in order after the postings' own URLs and before a guess
_LOOKUPS = (("clearbit", _clearbit), ("wikidata", _wikidata))
# every way to a website, in the order resolve_domain tries them
METHODS = ("feed", "apply-url", *(method for method, _ in _LOOKUPS), "guess-verified")
# the methods that read a posting's own URLs and make no request
_LOCAL_METHODS = METHODS[:2]

# What resolve_domain found. method names the way to domain, or with no domain gives
# every step's reason. transient says every failure may pass on a later try.
Resolution = collections.namedtuple("Resolution", "domain method transient")


def resolve_domain(company_name, sample_urls, company_url=None, deadline=None, after=None):
    """The Resolution of a company's website, first hit wins: the feed's company_url
    ("feed"), an apply URL on the company's own domain ("apply-url"), Clearbit's
    suggestion under the exact name ("clearbit"), the official website of the
    Wikidata organisation under the exact name ("wikidata"), or a guess from the name
    its home page confirms ("guess-verified"). Given `after`, a method, only the ones
    after it are tried. No request outlives `deadline`, by default ITEM_SECONDS from
    now, and running out of time counts as a failure that may pass."""
    methods = METHODS[METHODS.index(after) + 1:] if after else METHODS
    for method, urls in (("feed", [company_url]), ("apply-url", sample_urls)):
        domain = next(filter(None, map(_company_site, urls)), None)
        if method in methods and domain:
            return Resolution(domain, method, False)
    deadline = deadline or time.monotonic() + ITEM_SECONDS
    name, reasons, transient = parse_name(company_name), [], []
    for method, lookup in _LOOKUPS:
        if method not in methods or not name.words:
            continue
        try:
            return Resolution(lookup(name, deadline), method, False)
        except Exception as e:  # noqa: BLE001 - a failed lookup moves on to the next
            reasons.append(f"{method}: {_reason(e)}")
            transient.append(_transient(e))
    lookups = len(transient)
    if "guess-verified" in methods:
        domain, why, guesses = _guess(name.text, deadline)
        if domain:
            return Resolution(domain, why, False)
        reasons.append(why)
        transient.extend(guesses)
    # a name lookup that could not answer, as on a 429, outweighs a rejected guess
    unanswered = any(transient[:lookups])
    return Resolution(None, "; ".join(reasons) or f"no way to a website after {after}",
                      unanswered or _may_pass(transient, deadline))


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


def _due(row, now):
    return row is None or (row["path"] is None and row["fetched_at"] < now - RETRY_AFTER)


def _pooled(work, items, deadline):
    """(item, work(item, its deadline), whether its deadline was `deadline`) for each
    item finished by the deadline, in item order.

    Each item gets ITEM_SECONDS and none past `deadline`, and no request outlives
    its item's deadline, so work still running when the pool is abandoned ends by
    then; only a DNS lookup, which takes no timeout, can run longer. Abandoned work
    only reads the network and writes icon files, and its result goes unrecorded.
    """
    def run(item):
        item_deadline = min(deadline, time.monotonic() + ITEM_SECONDS)
        return work(item, item_deadline), item_deadline == deadline

    pool = ThreadPoolExecutor(WORKERS)
    futures = [pool.submit(run, item) for item in items]
    try:
        wait(futures, timeout=max(0, deadline - time.monotonic()))
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    return [(item, *future.result()) for item, future in zip(items, futures)
            if future.done() and not future.cancelled()]


# The companies, icon domains, and moves to a later website that one fetch_all has
# tried, so a failure that may pass is not tried again until the next fetch. An item
# the pass's deadline cut short stays out, and the next pass tries it.
Tried = collections.namedtuple("Tried", "companies domains moves")


def _tried():
    return Tried(set(), set(), set())


def _resolve(company, deadline):
    _, name, company_url, urls = company
    return resolve_domain(name, urls, company_url, deadline)


def _resolve_companies(limit, deadline, tried, keys):
    """Look up the websites of up to `limit` companies, recording each outcome but a
    failure that may pass. Returns how many were looked up, (company, domain, method)
    for each found, and whether each lookup that made a request failed in a way that
    may pass."""
    found, outcomes = [], []
    companies = store.companies_without_domain(limit, tried.companies, keys)
    looked_up = _pooled(_resolve, companies, deadline)
    for company, (domain, method, transient), cut_short in looked_up:
        if not (transient and cut_short):
            tried.companies.add(company[0])
        if not (domain and method in _LOCAL_METHODS):
            outcomes.append(transient)
        if domain:
            store.save_company(company[0], domain, method)
            found.append((company, domain, method))
        elif not transient:
            store.save_company(company[0], None, None, method)
    return len(looked_up), found, outcomes


def _iconless(found, limit, skip, keys):
    """Up to `limit` known websites not in skip with no icon and no recent failure,
    the ones just found first."""
    recorded, now = store.logos(), time.time()
    domains = dict.fromkeys([*found, *store.company_domains(keys)])
    return [domain for domain in domains
            if domain not in skip and _due(recorded.get(domain), now)][:limit]


def _download_icons(domains, deadline, tried):
    """Download the icons of `domains`, recording each outcome. Returns (fetched,
    failed, whether each download failed in a way that may pass)."""
    fetched = failed = 0
    outcomes = []
    for domain, download, cut_short in _pooled(_icon, domains, deadline):
        _record(domain, download)
        if not (download.transient and cut_short):
            tried.domains.add(domain)
        fetched, failed = fetched + bool(download.path), failed + (not download.path)
        outcomes.append(download.transient)
    return fetched, failed, outcomes


# The outcome of _later_site for a company. domain and method name the website it
# moves to, both None when there is none. tries holds (domain, Download) for each
# download, reasons why each later website was passed over, and transient whether
# any of those may pass on a later try.
Move = collections.namedtuple("Move", "domain method tries reasons transient")


def _later_site(company, deadline, recorded):
    """The Move of a company whose website has no icon to the first website a later
    method gives whose icon is in `recorded` or downloads."""
    _, name, company_url, urls, domain, method = company
    seen, tries, reasons, transient = {domain}, [], [], False
    while True:
        found = resolve_domain(name, urls, company_url, deadline, after=method)
        if found.domain is None:
            reasons.append(found.method)
            return Move(None, None, tries, reasons, transient or found.transient)
        domain, method = found.domain, found.method
        row = recorded.get(domain)
        if domain in seen or (row and row["path"] is None):
            reasons.append(f"{method}: {domain} has no icon")
            continue
        if row:
            return Move(domain, method, tries, reasons, False)
        seen.add(domain)
        download = _icon(domain, deadline)
        tries.append((domain, download))
        if download.path:
            return Move(domain, method, tries, reasons, False)
        reasons.append(f"{method}: {domain}: {download.error}")
        transient = transient or download.transient


def _move_iconless(limit, deadline, tried, keys):
    """Move up to `limit` companies whose website has no icon to the first website a
    later method gives that has one, recording every download. apply-url found
    lifeattiktok.com for TikTok, which no icon source has, ahead of Clearbit's
    tiktok.com. A company no later method moves keeps its website, with error set,
    and is not tried again for COMPANY_RETRY_DAYS. Returns (moved, fetched, failed,
    whether each try failed in a way that may pass)."""
    moved = fetched = failed = 0
    outcomes = []
    companies = store.companies_without_icon(limit, METHODS[-1], tried.moves, keys)
    later_site = functools.partial(_later_site, recorded=store.logos())
    for company, move, cut_short in _pooled(later_site, companies, deadline):
        key, *_, domain, method = company
        for tried_domain, download in move.tries:
            _record(tried_domain, download)
            fetched, failed = fetched + bool(download.path), failed + (not download.path)
        if not (move.transient and cut_short):
            tried.moves.add(key)
        outcomes.append(move.transient)
        if move.domain:
            store.save_company(key, move.domain, move.method)
            moved += 1
        elif not move.transient:
            store.save_company(key, domain, method,
                               f"no icon at {domain}; later steps: {'; '.join(move.reasons)}")
    return moved, fetched, failed, outcomes


class IconsRunning(Exception):
    """Another icon fetch holds the icons.pid marker."""

    def __init__(self):
        super().__init__("a company icon fetch is already running")


def _marker():
    return paths.run_lock().with_name("icons.pid")


# a file lock, which the system releases when its holder dies, so a marker left by
# a process that died holds nothing
@contextlib.contextmanager
def icon_job():
    """Hold the icons.pid marker, which one icon fetch at a time holds across
    processes and threads. Raises IconsRunning when another fetch holds it."""
    marker = _marker()
    marker.parent.mkdir(parents=True, exist_ok=True)
    fd = locks.open_file(marker, os.O_CREAT | os.O_RDWR)
    try:
        if not locks.acquire(fd):
            raise IconsRunning()
        locks.write_pid(fd)
        try:
            yield
        finally:
            locks.release(fd)
    finally:
        os.close(fd)


def icons_running():
    """Whether an icon fetch holds the icons.pid marker, here or in another process."""
    try:
        fd = locks.open_file(_marker(), os.O_RDONLY)
    except FileNotFoundError:
        return False
    try:
        if not locks.acquire(fd, shared=True):
            return True
        locks.release(fd)
        return False
    finally:
        os.close(fd)


def _offline(outcomes):
    """Whether the network looks down, given whether each request-making attempt
    failed in a way that may pass."""
    return len(outcomes) >= OFFLINE_ATTEMPTS and all(outcomes)


# The outcome of one fetch_missing pass, counting companies looked up, resolved, and
# moved to a later website, icons fetched and failed, and the companies left to look
# up. offline says the network looked down.
Pass = collections.namedtuple("Pass", "looked_up resolved moved fetched failed remaining "
                                      "offline")


def _pass(limit, budget_seconds, tried, keys=None):
    """The work of fetch_missing, over the companies keyed in keys, or every company
    when keys is None. Returns the Pass and (company, domain, method) for each
    website found."""
    start = time.monotonic()
    end = start + budget_seconds
    looked_up, found, outcomes = _resolve_companies(
        limit, start + budget_seconds * RESOLVE_SHARE, tried, keys)
    moved = fetched = failed = 0
    if not _offline(outcomes):
        fetched, failed, downloads = _download_icons(
            _iconless([domain for _, domain, _ in found], limit, tried.domains, keys), end,
            tried)
        moved, moved_fetched, moved_failed, moves = _move_iconless(limit, end, tried, keys)
        fetched, failed = fetched + moved_fetched, failed + moved_failed
        outcomes += downloads + moves
    return Pass(looked_up, len(found), moved, fetched, failed,
                store.count_companies_without_domain(), _offline(outcomes)), found


def fetch_missing(limit=MAX_PER_RUN, budget_seconds=BUDGET_SECONDS, tried=None):
    """Find the websites of up to `limit` companies that have none, in the order
    store.companies_without_domain ranks them, until RESOLVE_SHARE of budget_seconds
    is spent, then fetch the icons they lack and move up to `limit` companies whose
    website has no icon to one a later method gives, until all of it is. Stops after
    the lookups when every one that made a request failed in a way that may pass.
    Leaves out what `tried`, a Tried, holds, and adds what it tries. Returns the
    Pass."""
    result, found = _pass(limit, budget_seconds, _tried() if tried is None else tried)
    methods = collections.Counter(method for *_, method in found)
    events.emit("info", text=(
        f"[logos] resolved {len(found)} companies (feed {methods['feed']}, "
        f"apply-url {methods['apply-url']}, clearbit {methods['clearbit']}, "
        f"wikidata {methods['wikidata']}, verified guess {methods['guess-verified']}), "
        f"moved {result.moved} to a later website, fetched {result.fetched} icons, "
        f"{result.failed} failed, {result.remaining} companies still to look up"))
    if result.offline:
        events.emit("warn", text=f"[logos] {OFFLINE}, stopping")
    return result


def fetch_wanted(wanted, budget_seconds=WANTED_BUDGET):
    """Look up the websites and icons of the companies in `wanted`, a Wanted.take(),
    within budget_seconds, as a fetch_missing pass over them alone. Returns {name:
    website} for each name whose company's icon is stored."""
    _pass(len(wanted), budget_seconds, _tried(), list(wanted))
    stored = store.icon_domains(list(wanted))
    return {name: stored[key] for key, names in wanted.items() if key in stored
            for name in names}


class Wanted:
    """The companies a page shows with no icon, queued by name_key for a lookup soon.
    One worker thread, started when the queue fills, calls fetch(self) until the
    queue is empty, and fetch takes the batch. A company is queued once in `again`
    seconds, and the queue holds `size` at most."""

    def __init__(self, fetch, size=WANTED_MAX, again=WANTED_AGAIN):
        self._fetch, self._size, self._again = fetch, size, again
        self._lock = threading.Lock()
        self._queued = {}  # name_key: the names it is shown under
        self._asked = {}  # name_key: the time.monotonic() it was last queued
        self._draining = False

    def add(self, names):
        """Queue the companies shown under `names`. Returns how many were queued."""
        now, queued = time.monotonic(), 0
        with self._lock:
            self._asked = {key: at for key, at in self._asked.items()
                           if now - at < self._again}
            for name in names:
                key = store.name_key(name)
                if key in self._queued:
                    self._queued[key].add(name)
                elif key and key not in self._asked and len(self._queued) < self._size:
                    self._queued[key], self._asked[key] = {name}, now
                    queued += 1
            start = bool(self._queued) and not self._draining
            self._draining = self._draining or start
        if start:
            threading.Thread(target=self._drain, name="wanted-icons", daemon=True).start()
        return queued

    def take(self):
        """Empty the queue. Returns what it held, {name_key: set of names}."""
        with self._lock:
            taken, self._queued = self._queued, {}
        return taken

    def _drain(self):
        try:
            while True:
                with self._lock:
                    if not self._queued:
                        self._draining = False
                        return
                try:
                    self._fetch(self)
                except Exception as e:  # noqa: BLE001 - the batch is dropped and reported
                    self.take()
                    events.emit("warn", text=f"[logos] fetching the icons of shown "
                                             f"companies failed: {type(e).__name__}: {e}")
        finally:
            store.close_thread()


# the outcome of fetch_all, the icons it fetched and whether the network looked down
Job = collections.namedtuple("Job", "fetched offline")


def fetch_all(progress=None, limit=None):
    """Run fetch_missing passes until one tries nothing new, the network looks down,
    or `limit` companies have been looked up. After each pass, progress(done,
    remaining) gets the companies looked up so far and those still to look up.
    Returns the Job."""
    done = fetched = 0
    tried = _tried()
    while limit is None or done < limit:
        before = sum(map(len, tried))
        result = fetch_missing(MAX_PER_RUN if limit is None
                               else min(MAX_PER_RUN, limit - done), tried=tried)
        done, fetched = done + result.looked_up, fetched + result.fetched
        if progress:
            progress(done, result.remaining)
        if result.offline:
            return Job(fetched, True)
        if sum(map(len, tried)) == before:
            break
    return Job(fetched, False)
