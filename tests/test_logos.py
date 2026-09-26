"""Company icons: found on the company's own site, stored once, served with no request.

Requests go to test_net's LocalWeb, which stands in for the web with every check
before the connection running as it would live.
"""
import json
import socket
import threading
import time
import unittest
from unittest import mock
from urllib.parse import urlencode

from helpers import temp_home
from test_net import PUBLIC_IP, LocalWeb, drip, resolver

from cairn import __version__, events, fetch, logos, net, store


def _png(width):
    """A PNG signature and IHDR chunk for a square image `width` pixels wide."""
    size = width.to_bytes(4, "big")
    return (b"\x89PNG\r\n\x1a\n" + b"\0\0\0\rIHDR" + size + size + b"\x08\x06\0\0\0"
            + b"\0" * 4)


def _ico(*widths):
    """An ICO header and a directory entry for each square image; 0 stands for 256."""
    return (b"\0\0\1\0" + len(widths).to_bytes(2, "little")
            + b"".join(bytes([width, width]) + b"\0" * 14 for width in widths))


PNG = _png(32)
# hosts asked by name, whatever the company
LOOKUP_HOSTS = ("autocomplete.clearbit.com", "www.wikidata.org")
ICON_SERVICE_HOSTS = ("icons.duckduckgo.com", "t0.gstatic.com")


class _WebTest(unittest.TestCase):
    """A test whose requests reach self.web, which serves self.web.replies."""

    ADDRESSES = None

    @classmethod
    def setUpClass(cls):
        cls.web = LocalWeb()
        cls.addClassCleanup(cls.web.close)

    def setUp(self):
        self.web.clear()
        self.enterContext(mock.patch.object(net, "_connect", self.web.connect))
        lookups = resolver(self.ADDRESSES)
        self.enterContext(lookups)
        self.looked_up = lookups.looked_up


def _page(head):
    return 200, {"Content-Type": "text/html"}, f"<html><head>{head}</head><body>hi</body></html>".encode()


def _image(body=PNG, kind="image/png"):
    return 200, {"Content-Type": kind}, body


def _redirect(location):
    return 302, {"Location": location}, b""


def _json(data):
    return 200, {"Content-Type": "application/json"}, json.dumps(data).encode()


def _clearbit_url(name):
    return logos._CLEARBIT + urlencode({"query": name})


def _wikidata_search_url(name):
    return logos._WIKIDATA + urlencode({"action": "wbsearchentities", "search": name,
                                        "language": "en", "format": "json", "limit": 5})


def _wikidata_claims_url(entity):
    return logos._WIKIDATA + urlencode({"action": "wbgetclaims", "entity": entity,
                                        "property": "P856", "format": "json"})


def _website_claims(url):
    return _json({"claims": {"P856": [{"mainsnak": {"datavalue": {"value": url}}}]}})


def _off(hosts, urls):
    """urls, less those on any of hosts."""
    return [url for url in urls if url.split("/")[2] not in hosts]


class DomainForTest(unittest.TestCase):
    def test_the_domain_is_the_one_the_store_joined_in(self):
        self.assertEqual(logos.domain_for({"logo_domain": "acme.com"}), "acme.com")
        self.assertIsNone(logos.domain_for({"company_url": "https://acme.com"}))


def _title(title):
    return _page(f"<title>{title}</title>")


def _site_name(name, title=""):
    return _page(f'<title>{title}</title><meta property="og:site_name" content="{name}">')


class ResolveDomainTest(_WebTest):
    def _resolve(self, name, urls=(), company_url=None, replies=None):
        self.web.replies, self.web.requested = replies or {}, []
        return logos.resolve_domain(name, list(urls), company_url)[:2]

    def _guessed(self):
        """The requests made after the lookups by name."""
        return _off(LOOKUP_HOSTS, self.web.requested)

    def test_a_company_url_off_the_job_sites_wins(self):
        self.assertEqual(self._resolve("Acme", ["https://jobs.acme.com/1"],
                                       "https://www.acmecorp.com/about"),
                         ("acmecorp.com", "feed"))

    def test_an_apply_url_on_the_companys_own_domain_is_next(self):
        self.assertEqual(self._resolve("Acme", ["https://boards.greenhouse.io/acme/jobs/1",
                                                "https://careers.acme.com/jobs/2"],
                                       "https://simplify.jobs/c/Acme"),
                         ("acme.com", "apply-url"))
        self.assertEqual(self.web.requested, [])

    def test_a_two_label_public_suffix_keeps_three_labels(self):
        self.assertEqual(self._resolve("Acme", ["https://jobs.acme.co.uk/1"]),
                         ("acme.co.uk", "apply-url"))

    def test_job_sites_forms_and_reserved_names_are_never_a_company_site(self):
        for url in ("https://acme.wd5.myworkdayjobs.com/x", "https://jobs.lever.co/acme/1",
                    "https://docs.google.com/forms/d/1", "https://www.linkedin.com/jobs/1",
                    "https://jobs.test/1", "https://example.com/1", "https://10.0.0.1/1",
                    "https://intranet.corp/1", "javascript:alert(1)"):
            with self.subTest(url):
                self.assertEqual(self._resolve("Acmeco", [url]), (
                    None, "clearbit: HTTP 404; wikidata: HTTP 404; acmeco.com: HTTP 404"))
        self.assertEqual(self._resolve(
            "Google", ["https://www.google.com/about/careers/applications/jobs/1"]),
            ("google.com", "apply-url"))

    def test_a_guess_is_kept_when_the_site_title_names_the_company(self):
        self.assertEqual(self._resolve("Jane Street Group, LLC",
                                       replies={"https://janestreet.com/": _title("Jane Street")}),
                         ("janestreet.com", "guess-verified"))

    def test_the_hyphenated_guess_follows_the_joined_one(self):
        self.assertEqual(self._resolve("Jane Street",
                                       replies={"https://jane-street.com/": _title("Jane Street")}),
                         ("jane-street.com", "guess-verified"))
        self.assertEqual(self._guessed(),
                         ["https://janestreet.com/", "https://jane-street.com/"])

    def test_a_name_ending_in_ai_or_labs_also_tries_the_rest_under_ai(self):
        self.assertEqual(self._resolve("Scale AI", replies={
            "https://scale.ai/": _site_name("Scale AI")}), ("scale.ai", "guess-verified"))
        self.assertEqual(self._resolve("Hooli Labs", replies={
            "https://hooli.ai/": _site_name("Hooli", "Hooli | Home")}),
            ("hooli.ai", "guess-verified"))

    def test_guesses_keep_a_name_whole_and_skip_job_sites(self):
        cases = {"Open AI": ["openai.com", "open-ai.com", "open.ai"],
                 "Figure AI": ["figureai.com", "figure-ai.com", "figure.ai"],
                 "Hooli Labs": ["hooli.com", "hooli.ai"],
                 "Initrode IO": ["initrodeio.com", "initrode-io.com", "initrode.io"],
                 "Two Sigma": ["twosigma.com", "two-sigma.com"],
                 "Workable": [], "Greenhouse IO": ["greenhouseio.com", "greenhouse-io.com"],
                 "Example": []}
        for name, guesses in cases.items():
            with self.subTest(name):
                self.assertEqual(logos._guesses(name), guesses)

    def test_a_job_site_guess_is_never_requested(self):
        self.assertEqual(self._resolve("Workable"), (
            None, "clearbit: HTTP 404; wikidata: HTTP 404; no guess from the name"))
        self.assertEqual(self._guessed(), [])

    def test_a_top_clearbit_suggestion_under_the_exact_name_is_taken(self):
        name = "Susquehanna International Group"
        self.assertEqual(self._resolve(name, replies={_clearbit_url(name): _json([
            {"name": "Susquehanna International Group,", "domain": "sig.com", "logo": None},
            {"name": "Susquehanna Bank", "domain": "susquehanna.net", "logo": None}])}),
            ("sig.com", "clearbit"))
        self.assertEqual(self.web.requested, [_clearbit_url(name)])

    def test_a_clearbit_suggestion_under_another_name_is_passed_over(self):
        domain, why = self._resolve("HRT", replies={_clearbit_url("HRT"): _json([
            {"name": "Hrvatska radiotelevizija", "domain": "hrt.hr", "logo": None},
            {"name": "HRT", "domain": "hrtchp.com", "logo": None}])})
        self.assertIsNone(domain)
        self.assertIn("clearbit: the top suggestion does not have the company's name", why)

    def test_a_clearbit_suggestion_off_a_companys_own_domain_is_passed_over(self):
        for name, domain in (("Workable", "workable.com"), ("Acme", "acme.github.io"),
                             ("Acme", "co.za")):
            with self.subTest(domain):
                found, why = self._resolve(name, replies={_clearbit_url(name): _json([
                    {"name": name, "domain": domain, "logo": None}])})
                self.assertIsNone(found)
                self.assertIn(f"clearbit: {domain} is not a company's own site", why)

    def test_a_clearbit_domain_is_taken_at_its_registrable_domain(self):
        for name, domain, site in (("Anduril", "andurilgroup.co.za", "andurilgroup.co.za"),
                                   ("Acme", "careers.acme.com", "acme.com")):
            with self.subTest(domain):
                self.assertEqual(self._resolve(name, replies={_clearbit_url(name): _json([
                    {"name": name, "domain": domain, "logo": None}])}), (site, "clearbit"))

    def test_a_clearbit_domain_must_hold_a_word_of_the_name_or_its_initials(self):
        cases = {"American Express": "americanexpress.com", "Koch Industries": "kochcareers.com",
                 "RTX": "rtx.com", "Texas Instruments": "ti.com",
                 "General Dynamics Mission Systems": "gdmissionsystems.com"}
        for name, domain in cases.items():
            with self.subTest(name):
                self.assertEqual(self._resolve(name, replies={_clearbit_url(name): _json([
                    {"name": name, "domain": domain, "logo": None}])}), (domain, "clearbit"))

    def test_a_clearbit_domain_with_no_part_of_the_name_is_passed_over(self):
        name = "GE Aerospace"
        domain, why = self._resolve(name, replies={_clearbit_url(name): _json([
            {"name": "GE Aerospace", "domain": "lexingtonmenus.com", "logo": None}])})
        self.assertIsNone(domain)
        self.assertIn("clearbit: lexingtonmenus.com holds no word of the name of 3 letters "
                      "or more, nor its initials", why)

    def test_a_trailing_parenthesised_part_is_left_out_of_every_lookup(self):
        name = "Susquehanna International Group (SIG)"
        self.assertEqual(logos.parse_name(name), logos.Name(
            "Susquehanna International Group", ["susquehanna", "international", "group"],
            {"sig"}))
        self.assertEqual(self._resolve(name, replies={
            _clearbit_url("Susquehanna International Group"): _json([
                {"name": "Susquehanna International Group,", "domain": "sig.com",
                 "logo": None}])}), ("sig.com", "clearbit"))
        domain, why = self._resolve("Lawrence Livermore National Laboratory (LLNL)")
        self.assertIn("lawrencelivermorenationallaboratory.com: HTTP 404", why)
        self.assertEqual(self._guessed(), ["https://lawrencelivermorenationallaboratory.com/",
                                           "https://lawrence-livermore-national-laboratory.com/"])

    def test_the_parenthesised_part_counts_as_the_names_initials(self):
        name = "Hudson River Trading (HRT)"
        self.assertEqual(logos.parse_name(name).initials, {"hrt"})
        self.assertEqual(self._resolve(name, replies={
            _clearbit_url("Hudson River Trading"): _json([
                {"name": "Hudson River Trading", "domain": "hrt.com", "logo": None}])}),
            ("hrt.com", "clearbit"))

    def test_only_the_methods_after_the_one_given_are_tried(self):
        name = "TikTok"
        replies = {_clearbit_url(name): _json([
            {"name": "TikTok", "domain": "tiktok.com", "logo": None}])}
        self.assertEqual(self._resolve(name, ["https://lifeattiktok.com/1"], replies=replies),
                         ("lifeattiktok.com", "apply-url"))
        self.web.requested = []
        self.assertEqual(logos.resolve_domain(name, ["https://lifeattiktok.com/1"],
                                              after="apply-url"),
                         ("tiktok.com", "clearbit", False))
        self.assertEqual(logos.resolve_domain(name, [], after="guess-verified"),
                         (None, "no way to a website after guess-verified", False))

    def test_a_wikidata_organisation_under_the_exact_name_gives_its_website(self):
        name = "84.51°"
        self.assertEqual(self._resolve(name, replies={
            _clearbit_url(name): _json([]),
            _wikidata_search_url(name): _json({"search": [
                {"id": "Q1", "label": "84.51° Stadium", "description": "football company"},
                {"id": "Q2", "label": "84.51°", "description": "American actor"},
                {"id": "Q3", "label": "84.51°",
                 "description": "American consumer analytics company"}]}),
            _wikidata_claims_url("Q3"): _website_claims("https://www.8451.com/")}),
            ("8451.com", "wikidata"))
        self.assertEqual(_off(("autocomplete.clearbit.com",), self.web.requested),
                         [_wikidata_search_url(name), _wikidata_claims_url("Q3")])

    def test_a_wikidata_organisation_without_a_website_is_rejected(self):
        name = "Booz Allen"
        domain, why = self._resolve(name, replies={
            _wikidata_search_url(name): _json({"search": [
                {"id": "Q1", "label": "Booz Allen", "description": "consulting firm"}]}),
            _wikidata_claims_url("Q1"): _json({"claims": {}})})
        self.assertIsNone(domain)
        self.assertIn("wikidata: Q1 has no official website", why)
        self.assertIn("boozallen.com: HTTP 404", why)

    def test_a_lookup_that_fails_moves_on_to_the_next(self):
        name = "Hooli"
        self.assertEqual(self._resolve(name, replies={
            _clearbit_url(name): (200, {"Content-Type": "application/json"}, b"[{"),
            _wikidata_search_url(name): _json({"search": [
                {"id": "Q9", "label": "Hooli", "description": "technology company"}]}),
            _wikidata_claims_url("Q9"): _website_claims("https://hooli.xyz")}),
            ("hooli.xyz", "wikidata"))

    def test_a_redirect_settles_on_the_final_domain_unless_it_is_a_job_site(self):
        self.assertEqual(self._resolve("Initech", replies={
            "https://initech.com/": _redirect("https://www.initech.io/home"),
            "https://www.initech.io/home": _title("Initech")}),
            ("initech.io", "guess-verified"))
        domain, why = self._resolve("Initech", replies={
            "https://initech.com/": _redirect("https://www.linkedin.com/company/initech"),
            "https://www.linkedin.com/company/initech": _title("Initech")})
        self.assertIsNone(domain)
        self.assertIn("redirected to www.linkedin.com", why)

    def test_a_one_word_name_needs_a_title_part_that_is_the_name_alone(self):
        domain, why = self._resolve("Prevail", replies={
            "https://prevail.com/": _title("Prevail Adult Incontinence Products | Prevail "
                                           "Protective Hygiene")})
        self.assertIsNone(domain)
        self.assertIn("does not name the company", why)
        self.assertEqual(self._resolve("Initech", replies={
            "https://initech.com/": _title("Initech | Workplace platform")}),
            ("initech.com", "guess-verified"))
        self.assertEqual(self._resolve("Anthropic", replies={
            "https://anthropic.com/": _title("Home \\ Anthropic")}),
            ("anthropic.com", "guess-verified"))

    def test_a_short_one_word_name_needs_og_site_name(self):
        for name, title in (("Block", "Block | Lego fan site"), ("Envoy", "Envoy | Home")):
            with self.subTest(name):
                url = f"https://{name.lower()}.com/"
                self.assertIsNone(self._resolve(name, replies={url: _title(title)})[0])
                self.assertEqual(self._resolve(name, replies={url: _site_name(name, title)}),
                                 (f"{name.lower()}.com", "guess-verified"))

    def test_titles_that_share_words_with_the_name_are_rejected(self):
        cases = {"HRT": ("https://hrt.com/", "Hormone Replacement Clinic"),
                 "Jane Street": ("https://janestreet.com/", "Street art by Jane Doe"),
                 "Two Sigma": ("https://twosigma.com/", "Sigma Two | Fraternity"),
                 "Figure AI": ("https://figure.ai/", "Figure | Home Equity Line"),
                 "Scale AI": ("https://scale.ai/", "AI Scale reviews")}
        for name, (url, title) in cases.items():
            with self.subTest(name):
                domain, why = self._resolve(name, replies={url: _title(title)})
                self.assertIsNone(domain)
                self.assertIn("does not name the company", why)
                self.assertNotIn("https://figure.com/", self.web.requested)

    def test_a_multi_word_name_matches_spaced_run_together_or_punctuated(self):
        for title in ("Jane Street", "JaneStreet | Trading", "Welcome to Jane-Street"):
            with self.subTest(title):
                self.assertEqual(self._resolve("Jane Street", replies={
                    "https://janestreet.com/": _title(title)}),
                    ("janestreet.com", "guess-verified"))

    def test_a_parked_domain_naming_itself_is_rejected(self):
        domain, why = self._resolve("Initech", replies={
            "https://initech.com/": _title("initech.com is for sale")})
        self.assertIsNone(domain)
        self.assertIn("initech.com: title 'initech.com is for sale' does not name the company",
                      why)

    def test_a_multi_word_name_needs_every_word(self):
        domain, _ = self._resolve("Jane Street", replies={
            "https://janestreet.com/": _title("Street Food"),
            "https://jane-street.com/": _title("Jane's Blog")})
        self.assertIsNone(domain)

    def test_an_unreachable_guess_gives_the_reason(self):
        domain, why = self._resolve("Globex")
        self.assertIsNone(domain)
        self.assertIn("globex.com: HTTP 404", why)


class PublicWebOnlyTest(_WebTest):
    """No request reaches a host off the public internet, however it is named."""

    ADDRESSES = {"intranet.com": ["10.0.0.1"], "mixed.com": [PUBLIC_IP, "127.0.0.1"],
                 "mapped.com": ["::ffff:10.0.0.1"]}

    def setUp(self):
        super().setUp()
        self.enterContext(temp_home())

    def _icon_error(self, domain):
        self.assertIsNone(logos.fetch(domain))
        return store.logo(domain)["error"]

    def test_an_icon_link_to_a_private_host_is_refused(self):
        self.web.replies = {"https://acme.com/": _page(
            '<link rel="icon" href="http://127.0.0.1:631/x.png" sizes="64x64">'
            '<link rel="icon" href="http://169.254.169.254/latest/meta-data/">')}
        self.assertIn("not a public host name", self._icon_error("acme.com"))
        self.assertEqual(_off(ICON_SERVICE_HOSTS, self.web.requested),
                         ["https://acme.com/favicon.ico", "https://acme.com/"])

    def test_a_host_resolving_to_any_private_address_is_refused(self):
        for domain in ("intranet.com", "mixed.com", "mapped.com"):
            with self.subTest(domain):
                self.assertIn("a private address", self._icon_error(domain))
        self.assertEqual(_off(ICON_SERVICE_HOSTS, self.web.requested), [])

    def test_a_redirect_to_a_private_host_ends_the_attempt(self):
        self.web.replies = {"https://acme.com/favicon.ico": _redirect("http://192.168.1.1/"),
                            "https://acme.com/": _page("")}
        self._icon_error("acme.com")
        self.assertEqual(_off(ICON_SERVICE_HOSTS, self.web.requested),
                         ["https://acme.com/favicon.ico", "https://acme.com/"])
        self.assertEqual(set(self.web.dialed), {PUBLIC_IP})
        domain, why, _ = logos.resolve_domain("Hooli", [], None)
        self.assertIsNone(domain)
        self.web.replies = {"https://hooli.com/": _redirect("https://intranet.com/")}
        domain, why, _ = logos.resolve_domain("Hooli", [], None)
        self.assertIn("hooli.com: intranet.com resolves to 10.0.0.1, a private address", why)

    def test_the_connection_goes_to_the_checked_address_named_by_host(self):
        answers = iter([[PUBLIC_IP], ["10.0.0.1"]])

        def rebinding(host, port, family=0, type=0, proto=0, flags=0):
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port))
                    for ip in next(answers)]
        self.web.replies = {"https://acme.com/favicon.ico": _image()}
        with mock.patch("socket.getaddrinfo", rebinding):
            self.assertIsNotNone(logos.fetch("acme.com"))
        self.assertEqual((self.web.dialed, self.web.hosts), ([PUBLIC_IP], ["acme.com"]))


class FetchTest(_WebTest):
    def setUp(self):
        super().setUp()
        self.home = self.enterContext(temp_home())

    def test_favicon_ico_is_stored_and_served_without_a_request(self):
        self.web.replies = {"https://acme.com/favicon.ico": _image()}
        path = logos.fetch("acme.com")
        self.assertEqual(path, self.home / "logos" / "acme.com.png")
        self.assertEqual(path.read_bytes(), PNG)
        self.web.requested.clear()
        self.assertEqual(logos.logo_file("acme.com"), path)
        self.assertEqual(self.web.requested, [])
        self.assertEqual(logos.content_type(path), "image/png")

    def test_a_missing_favicon_falls_back_to_the_largest_linked_icon(self):
        head = ('<link rel="icon" href="/small.png" sizes="16x16">'
                '<link rel="apple-touch-icon" href="img/touch.png" sizes="180x180">')
        self.web.replies = {"https://acme.com/": _page(head),
                            "https://acme.com/img/touch.png": _image()}
        path = logos.fetch("acme.com")
        self.assertEqual(path.read_bytes(), PNG)
        self.assertNotIn("https://acme.com/small.png", self.web.requested)

    def test_a_shortcut_icon_link_is_followed(self):
        self.web.replies = {"https://acme.com/": _page('<link rel="shortcut icon" href="f.ico">'),
                            "https://acme.com/f.ico": _image(b"\0\0\1\0", "image/x-icon")}
        self.assertEqual(logos.fetch("acme.com").suffix, ".ico")

    def test_a_reply_that_is_not_an_image_is_rejected(self):
        self.web.replies = {"https://acme.com/favicon.ico": _image(b"<html>", "text/html"),
                            "https://acme.com/": _page("")}
        self.assertIsNone(logos.fetch("acme.com"))
        self.assertIn("text/html is not an image", store.logo("acme.com")["error"])
        self.assertIsNone(logos.logo_file("acme.com"))
        self.assertFalse((self.home / "logos").exists())

    def test_an_oversize_icon_is_rejected(self):
        self.web.replies = {"https://acme.com/favicon.ico": _image(PNG + b"\0" * logos.MAX_BYTES),
                            "https://acme.com/": _page("")}
        self.assertIsNone(logos.fetch("acme.com"))
        self.assertIn("over 512 KiB", store.logo("acme.com")["error"])
        self.assertFalse((self.home / "logos").exists())

    def test_the_favicon_services_are_tried_when_the_site_has_no_icon(self):
        ddg = "https://icons.duckduckgo.com/ip3/acme.com.ico"
        gstatic = ("https://t0.gstatic.com/faviconV2?client=SOCIAL&type=FAVICON"
                   "&fallback_opts=TYPE,SIZE,URL&url=https://acme.com&size=128")
        self.web.replies = {"https://acme.com/": _page(""), ddg: _image(_ico(32), "image/x-icon")}
        self.assertEqual(logos.fetch("acme.com").read_bytes(), _ico(32))
        self.assertEqual(store.logo("acme.com")["source"], "duckduckgo")
        self.assertNotIn(gstatic, self.web.requested)
        # a 404 from DuckDuckGo carries a placeholder image, which is never kept
        self.web.replies = {ddg: (404, {"Content-Type": "image/png"}, _png(64)),
                            gstatic: _image()}
        self.assertEqual(logos.fetch("acme.com").read_bytes(), PNG)
        self.assertEqual(store.logo("acme.com")["source"], "gstatic")
        self.web.replies = {}
        self.assertIsNone(logos.fetch("acme.com"))
        self.assertIn(f"{gstatic}: HTTP 404", store.logo("acme.com")["error"])

    def test_a_16_pixel_favicon_gives_way_to_a_wider_icon(self):
        head = '<link rel="icon" href="/big.png" sizes="192x192">'
        self.web.replies = {"https://acme.com/favicon.ico": _image(_ico(16), "image/x-icon"),
                            "https://acme.com/": _page(head),
                            "https://acme.com/big.png": _image(_png(192))}
        self.assertEqual(logos.fetch("acme.com").read_bytes(), _png(192))
        self.assertEqual(store.logo("acme.com")["source"], "site")

    def test_a_16_pixel_favicon_is_kept_when_nothing_wider_is_found(self):
        self.web.replies = {"https://acme.com/favicon.ico": _image(_ico(16), "image/x-icon"),
                            "https://acme.com/": _page("")}
        self.assertEqual(logos.fetch("acme.com").read_bytes(), _ico(16))
        self.assertEqual(store.logo("acme.com")["source"], "site")
        self.assertIn("https://icons.duckduckgo.com/ip3/acme.com.ico", self.web.requested)

    def test_a_wider_favicon_is_taken_without_looking_further(self):
        for width in (32, 0):
            with self.subTest(width):
                self.web.requested = []
                self.web.replies = {
                    "https://acme.com/favicon.ico": _image(_ico(width), "image/x-icon")}
                self.assertEqual(logos.fetch("acme.com").read_bytes(), _ico(width))
                self.assertEqual(self.web.requested, ["https://acme.com/favicon.ico"])

    def test_a_network_failure_is_never_raised_and_leaves_the_icon_pending(self):
        with mock.patch.object(net, "_connect", side_effect=TimeoutError("timed out")):
            self.assertIsNone(logos.fetch("acme.com"))
        self.assertIsNone(store.logo("acme.com"))

    def test_a_busy_or_broken_icon_service_leaves_the_icon_pending(self):
        self.web.replies = {"https://acme.com/favicon.ico": (503, {}, b""),
                            "https://acme.com/": (500, {}, b""),
                            "https://icons.duckduckgo.com/ip3/acme.com.ico": (429, {}, b""),
                            logos._GSTATIC.format(domain="acme.com"): (502, {}, b"")}
        self.assertIsNone(logos.fetch("acme.com"))
        self.assertIsNone(store.logo("acme.com"))

    def test_a_missing_icon_is_recorded(self):
        self.assertIsNone(logos.fetch("acme.com"))
        self.assertIn("https://acme.com/favicon.ico: HTTP 404", store.logo("acme.com")["error"])

    def test_the_widest_image_of_an_ico_is_its_width(self):
        self.assertEqual(logos._width(_ico(16, 32)), 32)
        self.assertEqual(logos._width(_ico(48, 16, 0)), 256)
        self.assertIsNone(logos._width(b"GIF89a"))
        self.web.replies = {"https://acme.com/favicon.ico": _image(_ico(16, 32), "image/x-icon")}
        self.assertEqual(logos.fetch("acme.com").read_bytes(), _ico(16, 32))
        self.assertEqual(self.web.requested, ["https://acme.com/favicon.ico"])

    def test_a_new_icon_replaces_the_file_of_the_old_one(self):
        self.web.replies = {"https://acme.com/favicon.ico": _image(_ico(32), "image/x-icon")}
        old = logos.fetch("acme.com")
        self.web.replies = {"https://acme.com/favicon.ico": _image()}
        new = logos.fetch("acme.com")
        self.assertEqual((old.suffix, new.suffix), (".ico", ".png"))
        self.assertEqual(sorted(p.name for p in (self.home / "logos").iterdir()),
                         ["acme.com.png"])

    def test_every_request_names_the_app_and_its_version(self):
        self.web.replies = {"https://acme.com/": _page("")}
        logos.fetch("acme.com")
        self.assertEqual(self.web.agents,
                         {f"cairn/{__version__} (local desktop app)"})


def _posting(posting_id, company, url, posted, company_url=None):
    return {"id": posting_id, "company_name": company, "title": "Software Engineer",
            "url": url, "company_url": company_url, "active": True, "is_visible": True,
            "date_posted": posted}


class FetchMissingTest(_WebTest):
    REPLIES = {"https://acme.com/favicon.ico": _image(),
               "https://initech.com/": _title("Initech")}

    def setUp(self):
        super().setUp()
        self.enterContext(temp_home())
        now = time.time()
        store.upsert_postings([
            _posting("new", "Acme", "https://jobs.acme.com/1", now),
            _posting("mid", "Globex", "https://boards.greenhouse.io/globex/1", now - 10,
                     "https://globex.com"),
            _posting("old", "Initech", "https://jobs.lever.co/initech/1", now - 100),
            _posting("oldest", "Hooli", "https://jobs.lever.co/hooli/1", now - 200)], "feed")
        self.heard = []
        self.addCleanup(events.subscribe(self.heard.append))

    def _fetch(self, limit=40):
        self.web.replies, self.web.requested = self.REPLIES, []
        logos.fetch_missing(limit)
        return list(self.web.requested)

    def _line(self):
        return self.heard[-1].data["text"]

    def test_companies_are_resolved_and_their_icons_fetched(self):
        self._fetch()
        self.assertEqual(self._line(), "[logos] resolved 3 companies (feed 1, apply-url 1, "
                                       "clearbit 0, wikidata 0, verified guess 1), "
                                       "moved 0 to a later website, fetched 1 icons, 2 failed, "
                                       "0 companies still to look up")
        self.assertEqual(store.logo("acme.com")["source"], "site")
        self.assertEqual([store.company_domain(k) for k in ("acme", "globex", "initech")],
                         ["acme.com", "globex.com", "initech.com"])
        self.assertIsNone(store.company_domain("hooli"))
        self.assertEqual(store.get_posting("new")["logo_domain"], "acme.com")
        self.assertIsNone(store.get_posting("mid")["logo_domain"])

    def test_the_limit_takes_the_newest_postings_companies(self):
        self._fetch(2)
        self.assertEqual([store.company_domain(k) for k in ("acme", "globex", "initech")],
                         ["acme.com", "globex.com", None])
        self.assertEqual([c[0] for c in store.companies_without_domain(10)],
                         ["initech", "hooli"])

    def test_the_company_with_the_most_postings_is_looked_up_first(self):
        store.upsert_postings([
            _posting(f"hooli-{i}", "Hooli", f"https://jobs.lever.co/hooli/{i + 2}",
                     time.time() - 300) for i in range(2)], "feed")
        self._fetch(1)
        self.assertEqual(self.web.requested[:3], [
            _clearbit_url("Hooli"), _wikidata_search_url("Hooli"), "https://hooli.com/"])
        self.assertEqual([c[0] for c in store.companies_without_domain(10)],
                         ["acme", "globex", "initech"])

    def test_a_scored_company_is_looked_up_before_a_busier_one(self):
        store.upsert_postings([
            _posting(f"hooli-{i}", "Hooli", f"https://jobs.lever.co/hooli/{i + 2}",
                     time.time() - 300) for i in range(2)], "feed")
        store.save_scores([{"id": "old", "fit": 80, "tier": 70}])
        self._fetch(1)
        self.assertEqual(store.company_domain("initech"), "initech.com")
        self.assertNotIn(_clearbit_url("Hooli"), self.web.requested)

    def test_the_wanted_companies_alone_are_fetched_and_their_icons_named(self):
        self.web.replies, self.web.requested = self.REPLIES, []
        icons = logos.fetch_wanted({"acme": {"Acme", "ACME"}, "initech": {"Initech"}})
        self.assertEqual(icons, {"Acme": "acme.com", "ACME": "acme.com"})
        self.assertEqual(store.company_domain("initech"), "initech.com")
        self.assertEqual([c[0] for c in store.companies_without_domain(10)],
                         ["globex", "hooli"])
        self.assertFalse([url for url in self.web.requested
                          if "globex" in url or "hooli" in url], self.web.requested)
        self.assertEqual(self.heard, [])

    def test_nothing_is_retried_until_its_wait_is_over(self):
        self._fetch()
        self.assertEqual(self._fetch(), [])
        self.assertEqual(self._line(), "[logos] resolved 0 companies (feed 0, apply-url 0, "
                                       "clearbit 0, wikidata 0, verified guess 0), "
                                       "moved 0 to a later website, fetched 0 icons, 0 failed, "
                                       "0 companies still to look up")
        with store.connect() as conn:
            conn.execute("UPDATE companies SET resolved_at = '2000-01-01T00:00:00' "
                         "WHERE domain IS NULL")
        self.assertEqual(_off(LOOKUP_HOSTS, self._fetch()), ["https://hooli.com/"])
        later = time.time() + logos.RETRY_AFTER + 1
        with mock.patch("time.time", return_value=later):
            requested = self._fetch()
        self.assertIn("https://globex.com/favicon.ico", requested)
        self.assertNotIn("https://acme.com/favicon.ico", requested)

    def test_work_past_the_budget_is_left_for_the_next_run(self):
        release = threading.Event()
        self.addCleanup(release.set)

        def resolve(name, urls, company_url=None, deadline=None):
            release.wait(10)
            return logos.Resolution(None, "late", False)
        with mock.patch.object(logos, "resolve_domain", resolve):
            logos.fetch_missing(budget_seconds=0.1)
        self.assertEqual(self._line(), "[logos] resolved 0 companies (feed 0, apply-url 0, "
                                       "clearbit 0, wikidata 0, verified guess 0), "
                                       "moved 0 to a later website, fetched 0 icons, 0 failed, "
                                       "4 companies still to look up")
        self.assertEqual(len(store.companies_without_domain(10)), 4)

    def test_lookups_stop_early_and_leave_the_rest_of_the_budget_to_icons(self):
        release = threading.Event()
        self.addCleanup(release.set)
        store.save_company("acme", "acme.com", "apply-url")
        resolve_deadlines, icon_deadlines = [], []

        def resolve(name, urls, company_url=None, deadline=None, after=None):
            if after:
                return logos.Resolution(None, "no later website", False)
            resolve_deadlines.append(deadline)
            release.wait(10)
            return logos.Resolution(None, "late", False)

        def icon(domain, deadline):
            icon_deadlines.append(deadline)
            return logos.Download(None, "none", None, False)
        start = time.monotonic()
        with (mock.patch.object(logos, "resolve_domain", resolve),
              mock.patch.object(logos, "_icon", icon)):
            result = logos.fetch_missing(budget_seconds=1)
        self.assertEqual((result.looked_up, result.failed), (0, 1))
        self.assertTrue(all(d <= start + 0.61 for d in resolve_deadlines), resolve_deadlines)
        self.assertEqual(len(icon_deadlines), 1)
        self.assertGreater(icon_deadlines[0], start + 0.6)

    def test_every_request_has_the_budget_as_its_deadline(self):
        deadlines = []

        def resolve(name, urls, company_url=None, deadline=None):
            deadlines.append(deadline)
            return logos.Resolution(None, "none", False)
        start = time.monotonic()
        with mock.patch.object(logos, "resolve_domain", resolve):
            logos.fetch_missing(budget_seconds=2)
        self.assertEqual(len(deadlines), 4)
        self.assertTrue(all(start < d <= start + 2.1 for d in deadlines), deadlines)


class LaterSiteTest(_WebTest):
    """A company whose website has no icon moves to one a later method finds."""

    def setUp(self):
        super().setUp()
        self.enterContext(temp_home())
        store.upsert_postings([_posting("t", "TikTok", "https://lifeattiktok.com/job/1",
                                        time.time())], "feed")

    def _company(self):
        return dict(store.connect().execute(
            "SELECT domain, method, error FROM companies WHERE name_key = 'tiktok'").fetchone())

    def _clearbit_finds_tiktok(self, icon=True):
        self.web.replies = {
            _clearbit_url("TikTok"): _json([{"name": "TikTok", "domain": "tiktok.com",
                                             "logo": None}])}
        if icon:
            self.web.replies["https://tiktok.com/favicon.ico"] = _image()

    def test_the_company_takes_the_next_website_with_an_icon_in_the_same_pass(self):
        self._clearbit_finds_tiktok()
        result = logos.fetch_missing()
        self.assertEqual(self._company(),
                         {"domain": "tiktok.com", "method": "clearbit", "error": None})
        self.assertEqual(logos.logo_file("tiktok.com").read_bytes(), PNG)
        self.assertIsNone(store.logo("lifeattiktok.com")["path"])
        self.assertEqual(store.get_posting("t")["logo_domain"], "tiktok.com")
        self.assertEqual((result.resolved, result.moved, result.fetched, result.failed),
                         (1, 1, 1, 1))

    def test_a_company_resolved_in_an_earlier_pass_moves(self):
        store.save_company("tiktok", "lifeattiktok.com", "apply-url")
        store.save_logo("lifeattiktok.com", None, "HTTP 404")
        self._clearbit_finds_tiktok()
        result = logos.fetch_missing()
        self.assertEqual((result.looked_up, result.moved), (0, 1))
        self.assertEqual(self._company(),
                         {"domain": "tiktok.com", "method": "clearbit", "error": None})

    def test_the_company_keeps_its_website_when_no_later_method_finds_one(self):
        logos.fetch_missing()
        company = self._company()
        self.assertEqual((company["domain"], company["method"]),
                         ("lifeattiktok.com", "apply-url"))
        self.assertTrue(company["error"].startswith(
            "no icon at lifeattiktok.com; later steps: clearbit: HTTP 404; "
            "wikidata: HTTP 404; tiktok.com: HTTP 404"), company["error"])
        self.assertIsNotNone(store.logo("lifeattiktok.com")["error"])
        self.assertIn("https://tiktok.com/", self.web.requested)

    def test_a_company_already_tried_does_not_move_again(self):
        logos.fetch_missing()
        self._clearbit_finds_tiktok()
        self.web.requested = []
        result = logos.fetch_missing()
        self.assertEqual((result.moved, self.web.requested), (0, []))
        with store.connect() as conn:
            conn.execute("UPDATE companies SET resolved_at = '2000-01-01T00:00:00'")
        self.assertEqual(logos.fetch_missing().moved, 1)

    def test_a_move_that_failed_only_in_ways_that_may_pass_is_tried_next_pass(self):
        store.save_company("tiktok", "lifeattiktok.com", "apply-url")
        store.save_logo("lifeattiktok.com", None, "HTTP 404")
        self.web.replies = {_clearbit_url("TikTok"): (429, {}, b""),
                            _wikidata_search_url("TikTok"): (503, {}, b""),
                            "https://tiktok.com/": (503, {}, b"")}
        self.assertEqual(logos.fetch_missing().moved, 0)
        self.assertIsNone(self._company()["error"])
        self._clearbit_finds_tiktok()
        self.assertEqual(logos.fetch_missing().moved, 1)

    def test_a_later_website_whose_icon_failed_before_is_passed_over(self):
        store.save_logo("tiktok.com", None, "HTTP 404")
        self._clearbit_finds_tiktok(icon=False)
        logos.fetch_missing()
        company = self._company()
        self.assertEqual((company["domain"], company["method"]),
                         ("lifeattiktok.com", "apply-url"))
        self.assertIn("clearbit: tiktok.com has no icon", company["error"])
        self.assertNotIn("https://tiktok.com/favicon.ico", self.web.requested)


class TransientTest(_WebTest):
    """A failure that may pass on a later try leaves the company or icon pending."""

    def setUp(self):
        super().setUp()
        self.enterContext(temp_home())
        self.heard = []
        self.addCleanup(events.subscribe(self.heard.append))

    def _hooli(self, site):
        store.upsert_postings([_posting("h", "Hooli", "https://jobs.lever.co/hooli/1",
                                        time.time())], "feed")
        self.web.replies = {_clearbit_url("Hooli"): (429, {}, b""),
                            _wikidata_search_url("Hooli"): (503, {}, b""),
                            "https://hooli.com/": site}

    def _company_rows(self):
        return store.connect().execute("SELECT count(*) FROM companies").fetchone()[0]

    def test_a_429_from_clearbit_falls_through_to_the_next_step(self):
        self._hooli((404, {}, b""))
        self.web.replies[_wikidata_search_url("Hooli")] = _json({"search": [
            {"id": "Q9", "label": "Hooli", "description": "technology company"}]})
        self.web.replies[_wikidata_claims_url("Q9")] = _website_claims("https://hooli.xyz")
        logos.fetch_missing()
        self.assertEqual(store.company_domain("hooli"), "hooli.xyz")

    def test_a_company_whose_every_step_may_pass_gets_no_row(self):
        self._hooli((503, {}, b""))
        found = logos.resolve_domain("Hooli", [])
        self.assertEqual(found, (None, "clearbit: HTTP 429; wikidata: HTTP 503; "
                                       "hooli.com: HTTP 503", True))
        logos.fetch_missing()
        self.assertEqual(self._company_rows(), 0)
        self.assertEqual([c[0] for c in store.companies_without_domain(10)], ["hooli"])
        self.assertEqual(store.icon_counts()["pending"], 1)

    def test_a_404_from_the_site_is_definitive_once_the_lookups_answered(self):
        self._hooli((404, {}, b""))
        self.web.replies[_clearbit_url("Hooli")] = _json([])
        self.web.replies[_wikidata_search_url("Hooli")] = _json({"search": []})
        self.assertTrue(logos.resolve_domain("Hooli", [])[0] is None)
        self.assertFalse(logos.resolve_domain("Hooli", []).transient)
        logos.fetch_missing()
        self.assertEqual(self._company_rows(), 1)
        self.assertEqual(store.companies_without_domain(10), [])
        self.assertEqual(store.icon_counts()["failed"], 1)

    def test_a_rejected_guess_stays_pending_while_a_lookup_could_not_answer(self):
        self._hooli((404, {}, b""))
        self.assertTrue(logos.resolve_domain("Hooli", []).transient)
        logos.fetch_missing()
        self.assertEqual(self._company_rows(), 0)
        self.assertEqual(store.icon_counts()["pending"], 1)

    def test_a_network_that_is_down_leaves_every_company_pending_and_stops(self):
        store.upsert_postings([
            _posting(f"p{i}", f"Company {i}", f"https://jobs.lever.co/c{i}/1", time.time())
            for i in range(8)] + [
            _posting("own", "Acme", "https://jobs.acme.com/1", time.time())], "feed")

        def down(host, port, family=0, type=0, proto=0, flags=0):
            raise socket.gaierror(socket.EAI_NONAME, "nodename nor servname provided")
        passes = []
        fetch_missing = logos.fetch_missing

        def counted(*args, **kwargs):
            passes.append(fetch_missing(*args, **kwargs))
            return passes[-1]
        with (mock.patch("socket.getaddrinfo", down),
              mock.patch.object(logos, "fetch_missing", counted)):
            job = logos.fetch_all()
        self.assertEqual(job, (0, True))
        self.assertEqual([(p.looked_up, p.offline) for p in passes], [(9, True)])
        self.assertEqual([tuple(row) for row in store.connect().execute(
            "SELECT name_key, domain FROM companies")], [("acme", "acme.com")])
        self.assertEqual(len(store.companies_without_domain(20)), 8)
        self.assertEqual(self.heard[-1].kind, "warn")
        self.assertEqual(self.heard[-1].data["text"], "[logos] network unavailable, stopping")
        self.assertEqual(store.logos(), {})

    def test_fewer_than_five_failed_lookups_are_not_an_outage(self):
        self._hooli((503, {}, b""))
        self.assertFalse(logos.fetch_missing().offline)

    def test_a_company_that_failed_in_a_way_that_may_pass_is_not_retried_in_the_same_fetch(self):
        self._hooli((503, {}, b""))
        self.assertEqual(logos.fetch_all(), (0, False))
        self.assertEqual(self.web.requested.count("https://hooli.com/"), 1)

    def test_a_download_the_pass_deadline_cut_short_leaves_no_row(self):
        store.save_company("acme", "acme.com", "feed")
        store.upsert_postings([_posting("a", "Acme", "https://jobs.acme.com/1",
                                        time.time())], "feed")
        self.web.replies = {"https://acme.com/": drip(b"HTTP/1.1 200 OK\r\nX-Slow: ")}
        logos.fetch_missing(budget_seconds=0.5)
        self.assertIsNone(store.logo("acme.com"))
        self.web.replies = {"https://acme.com/favicon.ico": _image()}
        logos.fetch_missing()
        self.assertEqual(store.logo("acme.com")["source"], "site")


class RegistrableTest(unittest.TestCase):
    def test_a_generic_second_level_label_keeps_three_labels(self):
        for host, domain in (("www.standardbank.co.za", "standardbank.co.za"),
                             ("samsung.com.cn", "samsung.com.cn"),
                             ("jobs.acme.co.uk", "acme.co.uk"),
                             ("leeds.nhs.uk", "leeds.nhs.uk"),
                             ("jobs.apple.com", "apple.com")):
            with self.subTest(host):
                self.assertEqual(logos.registrable(host), domain)

    def test_a_public_suffix_or_a_hosting_service_is_no_companys_domain(self):
        for host in ("co.za", "org.uk", "nhs.org.uk", "com.cn", "acme.github.io",
                     "x.vercel.app", "acme.netlify.app", "acme.herokuapp.com",
                     "acme.pages.dev", "acme.wixsite.com", "acme.squarespace.com",
                     "acme.webflow.io", "acme.myshopify.com", "acme.notion.site",
                     "acme.carrd.co", "acme.wordpress.com", "acme.blogspot.com",
                     "acme.weebly.com", "acme.godaddysites.com"):
            with self.subTest(host):
                self.assertIsNone(logos.registrable(host))
                self.assertIsNone(logos._company_site(f"https://{host}/"))


class NameRulesTest(_WebTest):
    def _resolve(self, name, replies):
        self.web.replies = replies
        return logos.resolve_domain(name, [])

    def _wikidata(self, name, label, site, description="technology company"):
        return {_wikidata_search_url(name): _json({"search": [
                    {"id": "Q1", "label": label, "description": description}]}),
                _wikidata_claims_url("Q1"): _website_claims(site)}

    def test_every_lookup_rejects_a_public_suffix_or_hosting_service(self):
        for url in ("https://co.za/", "https://acme.github.io/", "https://x.vercel.app/"):
            with self.subTest(url):
                self.assertEqual(self._resolve("Acme", {
                    **self._wikidata("Acme", "Acme", url),
                    "https://acme.com/": _redirect(url), url: _title("Acme")})[:2], (None, (
                        "clearbit: HTTP 404; wikidata: Q1 has no official website; "
                        f"acme.com: redirected to {url.split('/')[2]}")))
        self.web.replies = {}
        self.assertEqual(logos.resolve_domain("Acme", ["https://acme.github.io/jobs/1"],
                                              "https://acme.vercel.app/")[1],
                         "clearbit: HTTP 404; wikidata: HTTP 404; acme.com: HTTP 404")

    def test_wikidata_keeps_three_labels_under_a_generic_second_level_label(self):
        self.assertEqual(self._resolve("Standard Bank", self._wikidata(
            "Standard Bank", "Standard Bank", "https://www.standardbank.co.za/",
            "South African bank"))[:2], ("standardbank.co.za", "wikidata"))

    def test_a_name_with_more_words_than_the_company_is_not_its_name(self):
        _, why, _ = self._resolve("Figure", {
            _clearbit_url("Figure"): _json([{"name": "Figure Technologies",
                                             "domain": "figure.com"}]),
            **self._wikidata("Figure", "Figure Technologies", "https://figure.com/")})
        self.assertIn("clearbit: the top suggestion does not have the company's name; "
                      "wikidata: no organisation has the company's name", why)

    def test_a_legal_form_is_no_part_of_the_name(self):
        name = "Figure Technologies, Inc."
        self.assertEqual(self._resolve(name, {_clearbit_url(name): _json([
            {"name": "Figure Technologies", "domain": "figure.com"}])})[:2],
            ("figure.com", "clearbit"))

    def test_a_wikidata_website_must_spell_part_of_the_name(self):
        _, why, _ = self._resolve("Hooli", self._wikidata("Hooli", "Hooli",
                                                          "https://www.nucleus.com/"))
        self.assertIn("wikidata: Q1's website nucleus.com holds no word of the name", why)

    def test_initials_take_the_words_less_the_legal_form_and_two_letters_or_more(self):
        self.assertEqual(logos.parse_name("Apple Inc").initials, set())
        self.assertEqual(logos.parse_name("Texas Instruments Inc").initials, {"ti"})
        _, why, _ = self._resolve("Apple Inc", {_clearbit_url("Apple Inc"): _json([
            {"name": "Apple", "domain": "a.com"}])})
        self.assertIn("clearbit: a.com holds no word of the name", why)

    def test_a_parenthesised_part_is_initials_only_in_2_to_6_capitals(self):
        cases = {"Susquehanna International Group (SIG)": {"sig"},
                 "Hudson River Trading (HRT)": {"hrt"},
                 "Acme Robotics (Remote)": {"ar"}, "Acme (Remote)": set(),
                 "Acme (sig)": set(), "Acme (ABCDEFG)": set(), "Acme (A B)": set()}
        for name, initials in cases.items():
            with self.subTest(name):
                self.assertEqual(logos.parse_name(name).initials, initials)
        self.assertEqual(logos.parse_name("Acme Robotics (Remote)").text, "Acme Robotics")

    def test_a_malformed_reply_is_a_definitive_miss_with_a_plain_reason(self):
        cases = {"domain null": ([{"name": "Hooli", "domain": None}],
                                 "clearbit: the top suggestion has no domain"),
                 "domain a number": ([{"name": "Hooli", "domain": 42}],
                                     "clearbit: the top suggestion has no domain"),
                 "an object": ({"name": "Hooli", "domain": "hooli.com"},
                               "clearbit: the reply is not a list of suggestions"),
                 "a list of strings": (["hooli.com"],
                                       "clearbit: the top suggestion does not have the "
                                       "company's name")}
        for case, (reply, reason) in cases.items():
            with self.subTest(case):
                found = self._resolve("Hooli", {_clearbit_url("Hooli"): _json(reply),
                                                _wikidata_search_url("Hooli"): _json([1])})
                self.assertIsNone(found.domain)
                self.assertIn(reason, found.method)
                self.assertIn("wikidata: the reply holds no search results", found.method)
                self.assertNotIn("Error", found.method)
                self.assertFalse(found.transient)
        found = self._resolve("Hooli", {
            _wikidata_search_url("Hooli"): _json({"search": [
                "Q1", {"id": "Q2", "label": "Hooli", "description": "company"}]}),
            _wikidata_claims_url("Q2"): _json({"claims": {"P856": [{"mainsnak": None}]}})})
        self.assertIn("wikidata: Q2 has no official website", found.method)


class FetchAllTest(unittest.TestCase):
    def _fetch_all(self, passes, limit=None):
        calls, heard = [], []

        def fetch_missing(limit, tried):
            calls.append(limit)
            result, tries = passes.pop(0)
            tried.companies.update(f"{len(calls)}-{i}" for i in range(tries))
            return result
        with mock.patch.object(logos, "fetch_missing", fetch_missing):
            job = logos.fetch_all(lambda *args: heard.append(args), limit=limit)
        return job, calls, heard

    def test_passes_run_until_one_tries_nothing_new(self):
        job, calls, heard = self._fetch_all([
            (logos.Pass(150, 40, 2, 30, 10, 200, False), 150),
            (logos.Pass(150, 0, 0, 0, 5, 50, False), 150),
            (logos.Pass(0, 0, 0, 0, 0, 50, False), 0),
            (logos.Pass(9, 9, 0, 9, 0, 0, False), 9)])
        self.assertEqual(job, (30, False))
        self.assertEqual(calls, [logos.MAX_PER_RUN] * 3)
        self.assertEqual(heard, [(150, 200), (300, 50), (300, 50)])

    def test_a_limit_caps_the_companies_looked_up(self):
        with mock.patch.object(logos, "MAX_PER_RUN", 3):
            job, calls, heard = self._fetch_all(
                [(logos.Pass(3, 3, 0, 3, 0, 9, False), 3),
                 (logos.Pass(1, 1, 0, 1, 0, 8, False), 1)], limit=4)
        self.assertEqual((job, calls, heard), ((4, False), [3, 1], [(3, 9), (4, 8)]))

    def test_a_pass_that_finds_the_network_down_ends_the_fetch(self):
        job, calls, _ = self._fetch_all([(logos.Pass(150, 0, 0, 0, 0, 400, True), 150),
                                         (logos.Pass(150, 9, 0, 9, 0, 250, False), 150)])
        self.assertEqual((job, len(calls)), ((0, True), 1))


def _until(condition):
    deadline = time.monotonic() + 5
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError("timed out")
        time.sleep(0.01)


class WantedTest(unittest.TestCase):
    """The queue of companies shown with no icon, drained by one worker thread."""

    def setUp(self):
        self.enterContext(temp_home())
        self.batches, self.threads = [], set()
        self.release = threading.Event()
        self.addCleanup(self.release.set)

    def _fetch(self, wanted):
        self.release.wait(5)
        self.threads.add(threading.current_thread())
        self.batches.append(wanted.take())

    def test_a_company_is_queued_once_under_every_name_it_is_shown_under(self):
        queue = logos.Wanted(self._fetch)
        self.assertEqual(queue.add(["Acme", "ACME ", "Globex", "", "Acme"]), 2)
        self.release.set()
        _until(lambda: self.batches)
        self.assertEqual(self.batches, [{"acme": {"Acme", "ACME "}, "globex": {"Globex"}}])

    def test_the_queue_holds_its_size_and_asks_again_only_after_its_wait(self):
        queue = logos.Wanted(self._fetch, size=2, again=0.2)
        self.assertEqual(queue.add(["Acme", "Globex", "Initech"]), 2)
        self.release.set()
        _until(lambda: self.batches)
        self.assertEqual(queue.add(["Acme", "Initech"]), 1)
        time.sleep(0.2)
        self.assertEqual(queue.add(["Acme"]), 1)
        _until(lambda: len(self.batches) == 3)
        self.assertEqual(self.batches[1:], [{"initech": {"Initech"}}, {"acme": {"Acme"}}])

    def test_one_worker_drains_what_is_queued_while_it_runs(self):
        def fetch(wanted):
            self.batches.append(wanted.take())
            self.threads.add(threading.current_thread())
            self.release.wait(5)
        queue = logos.Wanted(fetch)
        queue.add(["Acme"])
        _until(lambda: self.batches)
        queue.add(["Globex"])
        self.release.set()
        _until(lambda: len(self.batches) == 2)
        self.assertEqual(self.batches, [{"acme": {"Acme"}}, {"globex": {"Globex"}}])
        self.assertEqual(len(self.threads), 1)

    def test_a_failed_batch_is_dropped_and_reported(self):
        heard = []
        self.addCleanup(events.subscribe(heard.append))

        def broken(wanted):
            raise RuntimeError("disk full")
        queue = logos.Wanted(broken)
        queue.add(["Acme"])
        _until(lambda: heard)
        self.assertEqual((heard[0].kind, heard[0].data["text"]),
                         ("warn", "[logos] fetching the icons of shown companies failed: "
                                  "RuntimeError: disk full"))
        self.assertEqual(queue.add(["Globex"]), 1)
        _until(lambda: len(heard) == 2)


class IconJobTest(unittest.TestCase):
    def setUp(self):
        self.home = self.enterContext(temp_home())

    def test_one_icon_fetch_at_a_time(self):
        self.assertFalse(logos.icons_running())
        with logos.icon_job():
            self.assertTrue(logos.icons_running())
            with self.assertRaises(logos.IconsRunning), logos.icon_job():
                pass
        self.assertFalse(logos.icons_running())

    def test_a_marker_left_by_a_process_that_died_holds_nothing(self):
        (self.home / "icons.pid").write_text("999999", encoding="utf-8")
        self.assertFalse(logos.icons_running())
        with logos.icon_job():
            self.assertTrue(logos.icons_running())


class SettingTest(unittest.TestCase):
    def test_a_run_fetches_no_icons_when_they_are_off(self):
        with (temp_home(company_icons=False),
              mock.patch.object(logos, "fetch_missing") as fetch_missing):
            fetch._fetch_icons()
        fetch_missing.assert_not_called()
        with temp_home(), mock.patch.object(logos, "fetch_missing") as fetch_missing:
            fetch._fetch_icons()
        fetch_missing.assert_called_once_with()

    def test_a_run_skips_icons_while_another_icon_fetch_runs(self):
        heard = []
        self.addCleanup(events.subscribe(heard.append))
        with (temp_home(), mock.patch.object(logos, "fetch_missing") as fetch_missing,
              logos.icon_job()):
            fetch._fetch_icons()
        fetch_missing.assert_not_called()
        self.assertEqual([(e.kind, e.data["text"]) for e in heard],
                         [("info", "[logos] an icon fetch is already running")])


if __name__ == "__main__":
    unittest.main()
