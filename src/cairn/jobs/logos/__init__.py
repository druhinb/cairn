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
from cairn.jobs.logos.icons import (
    MAX_BYTES,
    SMALL_ICON,
    Download,
    content_type,
    fetch,
    logo_file,
    path_for,
)
from cairn.jobs.logos.passes import (
    BUDGET_SECONDS,
    MAX_PER_RUN,
    OFFLINE,
    OFFLINE_ATTEMPTS,
    RESOLVE_SHARE,
    RETRY_AFTER,
    WANTED_AGAIN,
    WANTED_BUDGET,
    WANTED_MAX,
    WORKERS,
    IconsRunning,
    Job,
    Move,
    Pass,
    Tried,
    Wanted,
    fetch_all,
    fetch_missing,
    fetch_wanted,
    icon_job,
    icons_running,
)
from cairn.jobs.logos.web import HEADER_SECONDS, ITEM_SECONDS, REQUEST_SECONDS, NoIcon
from cairn.jobs.logos.websites import (
    AGGREGATORS,
    JSON_BYTES,
    METHODS,
    PAGE_BYTES,
    SHORT_NAME,
    Name,
    Resolution,
    domain_for,
    parse_name,
    registrable,
    resolve_domain,
)
