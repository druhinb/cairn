"""Where postings come from: GitHub listings feeds, list READMEs, Hacker News, YC,
RemoteOK, USAJOBS, public company job boards, and plain careers pages.

Every adapter returns rows in the shape store.upsert_postings takes, so the rest of
the pipeline never knows which kind of source a posting came from.
"""
from cairn.sources.boards import (
    BAMBOOHR_WORKERS,
    BOARD_MAX_BYTES,
    MAX_JOBS,
    WORKABLE_MAX_PAGES,
    ashby,
    bamboohr,
    greenhouse,
    lever,
    smartrecruiters,
    workable,
)
from cairn.sources.common import Skipped
from cairn.sources.github import LISTINGS_MAX_BYTES, github_listings, github_readme
from cairn.sources.hn import (
    HN_ITEM,
    HN_LATEST,
    HN_MAX_COMMENTS,
    HN_SEARCH,
    HN_WORKERS,
    hn_hiring,
    hn_thread,
)
from cairn.sources.job_sites import (
    REMOTEOK_API,
    USAJOBS_API,
    USAJOBS_SERIES,
    YC_HOST,
    YC_JOBS,
    YC_WORKERS,
    remoteok,
    usajobs,
    yc_waas,
)
from cairn.sources.pages import (
    PAGE_CONTEXT_LEVELS,
    PAGE_CONTEXT_RUNS,
    PAGE_MAX_BYTES,
    PAGE_MAX_LINKS,
    PAGE_MAX_TAGS,
    PAGE_TIMEOUT,
    page,
)
from cairn.sources.registry import BOARD_KINDS, KINDS, Source, fetch, fetch_all
from cairn.sources.resolve import RESOLVE_TIMEOUT, resolve_company, source_for_url
from cairn.sources.web import FEED_MAX_BYTES
from cairn.sources.workday_jobs import (
    WORKDAY_MAX_JOBS,
    WORKDAY_WORKERS,
    workday,
    workday_board,
)
