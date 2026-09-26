"""Passes over the companies with no icon, the icons.pid marker, and the queue of
companies a page shows.
"""
import collections
import contextlib
import functools
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, wait

from cairn import store
from cairn.core import events, locks, paths
from cairn.jobs.logos import icons, websites
from cairn.jobs.logos.icons import _record
from cairn.jobs.logos.web import ITEM_SECONDS
from cairn.jobs.logos.websites import _LOCAL_METHODS, METHODS

RETRY_AFTER = store.ICON_RETRY_DAYS * 86400
# 400 lookups by 12 workers took 72 s of a 90 s budget on 17k postings
MAX_PER_RUN = 400
BUDGET_SECONDS = 90
# the part of a pass's budget after which no new website lookup starts, which leaves
# the rest to icon downloads
RESOLVE_SHARE = 0.6
WORKERS = 12

# a pass whose requests, this many or more, all failed in a way that may pass finds the
# network down
OFFLINE_ATTEMPTS = 5
OFFLINE = "network unavailable"
# companies queued in a Wanted, the seconds before one is queued again, and the
# budget of the pass that fetches them
WANTED_MAX = 100
WANTED_AGAIN = 600
WANTED_BUDGET = 30


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
    return websites.resolve_domain(name, urls, company_url, deadline)


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
    for domain, download, cut_short in _pooled(icons._icon, domains, deadline):
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
        found = websites.resolve_domain(name, urls, company_url, deadline, after=method)
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
        download = icons._icon(domain, deadline)
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
