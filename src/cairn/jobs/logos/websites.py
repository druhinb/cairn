"""A company's website, from its postings, Clearbit, Wikidata, or a verified guess."""
import collections
import json
import re
import time
from html.parser import HTMLParser
from urllib.parse import urlencode, urlsplit

from cairn import store
from cairn.core import net
from cairn.jobs.logos.web import ITEM_SECONDS, _get, _may_pass, _reason, _transient

# the part of a home page searched for its title
PAGE_BYTES = 64 * 1024
JSON_BYTES = 64 * 1024

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
