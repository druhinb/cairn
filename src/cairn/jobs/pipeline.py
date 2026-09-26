"""One daily run as a library call: fetch, check apply links, rank, summarise.

Progress goes out through events.py, so a caller chooses how to show it. Postings
are marked seen only once their scores are stored, so a failed run retries in full.
"""
import dataclasses
import os
import time
from contextlib import contextmanager
from dataclasses import dataclass, field

from cairn import store, tracking
from cairn.core import events, locks, paths, settings
from cairn.jobs import descriptions, experience, fetch, rank
from cairn.tracking import applications


LINK_WAIT_SECONDS = 30
# A first run ranks every new posting, and ten summaries keep its model time to a
# few minutes on top of the ranking.
FIRST_RUN_SUMMARIES = 10


class UnknownPosting(LookupError):
    pass


class RunInProgress(Exception):
    def __init__(self, pid):
        holder = f" (pid {pid})" if pid is not None else ""
        super().__init__(f"a run is already in progress{holder}")
        self.pid = pid


@dataclass
class RunOptions:
    limit: int | None = None
    fit: int | None = None
    dry_run: bool = False
    first_run: bool = False


@dataclass
class RunResult:
    """`results` holds the ranked postings, or for a dry run the fetched ones;
    `follow_ups` the tracking.stale_applications() rows once a run has ranked."""
    run_id: int
    status: str
    counts: dict = field(default_factory=dict)
    results: list = field(default_factory=list)
    unranked: list = field(default_factory=list)
    elapsed: float = 0.0
    follow_ups: list = field(default_factory=list)


def run(opts, on_start=None):
    """Run the pipeline once under the run lock. Returns a RunResult.

    Raises RunInProgress when the lock is held elsewhere. A refused run emits no
    events and writes no runs row, so the caller reports the refusal itself. Any
    other failure is recorded on the run's row and re-raised. on_start(run_id) is
    called once the run has its row.
    """
    with run_lock(), run_settings(opts):
        return _recorded(opts, on_start)


def check():
    """Fetch the watchlist boards and rank what they list that is new, under the run
    lock and without a runs row. Returns (ranked postings, counts).

    Raises RunInProgress when the lock is held. The next run takes the scores as
    its own, so what a check found is listed with that run.
    """
    with run_lock():
        new, fetched = fetch.fetch_watchlist()
        new = experience.within_limit(new)
        results = _ranked(new, run_id=None)[0] if new else []
    return results, {"fetched": fetched, "new": len(new), "ranked": len(results)}


def summarise_posting(posting_id, force=False):
    """Distill the requirements of one stored posting. Returns the summary or None.

    A run summarises only the top max_summaries_per_run, so this is how a posting
    further down the list gets one. force fetches the posting again and replaces
    its summary. It writes only the descriptions table, so it needs no run lock.
    Raises UnknownPosting for an unknown id.
    """
    job = store.get_posting(posting_id)
    if not job:
        raise UnknownPosting(f"unknown posting id: {posting_id}")
    events.emit("summary_start", id=posting_id)
    try:
        keywords = descriptions.keywords(job, force=True) if force else descriptions.keywords(job)
    except Exception as e:
        events.emit("summary_done", id=posting_id, keywords=None,
                    err=f"{type(e).__name__}: {e}")
        raise
    events.emit("summary_done", id=posting_id, keywords=keywords,
                err=None if keywords else "no summary could be made from the posting")
    return keywords


@contextmanager
def run_settings(opts):
    """Settings with --fit and a first run's bounds applied, for this run only.

    A first run ranks past max_rank_per_run, which bounds the runs after it.

    Only settings.get() in this thread sees them; settings.base() and config.toml
    are never changed.
    """
    overrides = {}
    if opts.fit is not None:
        overrides["fit_threshold"] = opts.fit
    if opts.first_run:
        overrides["max_summaries_per_run"] = min(settings.get().max_summaries_per_run,
                                                 FIRST_RUN_SUMMARIES)
        overrides["max_rank_per_run"] = None
    with settings.override(dataclasses.replace(settings.get(), **overrides)):
        yield


# a file lock, released by the system when the holder dies. The earlier O_EXCL file plus
# pid-liveness check had two takeover races: a reader seeing the file before its pid
# was written, and two processes replacing the same dead pid.
@contextmanager
def run_lock():
    """Hold the run lock, which runs, seeding, and setup share across processes.

    Raises RunInProgress, carrying the holder's pid when the file names one.
    """
    lock = paths.run_lock()
    lock.parent.mkdir(parents=True, exist_ok=True)
    fd = locks.open_file(lock, os.O_CREAT | os.O_RDWR)
    try:
        if not locks.acquire(fd):
            raise RunInProgress(locks.read_pid(fd))
        locks.write_pid(fd)
        try:
            yield
        finally:
            locks.release(fd)
    finally:
        os.close(fd)


def lock_holder():
    """Pid of the process holding the run lock, or None when it is free.

    Also None while the holder has the lock but has not yet written its pid.
    """
    try:
        fd = locks.open_file(paths.run_lock(), os.O_RDONLY)
    except FileNotFoundError:
        return None
    try:
        if not locks.acquire(fd):
            return locks.read_pid(fd)
        locks.release(fd)
        return None
    finally:
        os.close(fd)


def _log_size(log):
    try:
        return log.stat().st_size
    except FileNotFoundError:
        return 0


def _recorded(opts, on_start):
    log = paths.run_log()
    log_start = _log_size(log)
    run_id = store.start_run(str(log))
    events.emit("run_start", run_id=run_id, **dataclasses.asdict(opts))
    try:
        if on_start:
            on_start(run_id)
        result = _pipeline(run_id, opts)
        store.finish_run(run_id, result.status, result.counts, log_start)
    except BaseException as e:
        events.emit("run_failed", run_id=run_id, error=f"{type(e).__name__}: {e}")
        _record_failure(run_id, log_start, _log_size(log))
        raise
    # run_done goes out only once the row is final, and its line still belongs in
    # the run's log range, so the range is closed after it
    events.emit("run_done", run_id=run_id, status=result.status, counts=result.counts,
                elapsed=result.elapsed)
    try:
        store.set_log_end(run_id, _log_size(log))
    except Exception as e:  # noqa: BLE001 - the run succeeded and its row is final
        events.emit("warn", text=f"could not record the log range of run {run_id}: {e}")
    return result


def _record_failure(run_id, log_start, log_end):
    """Mark the run failed without ever replacing the error that failed it."""
    try:
        store.finish_run(run_id, "failed", None, log_start, log_end)
    except Exception as e:  # noqa: BLE001 - the run's own error is what must propagate
        events.emit("warn", text=f"could not record run {run_id} as failed: {e}")


@contextmanager
def _phase(name):
    events.emit("phase_start", name=name)
    started = time.monotonic()
    yield
    events.emit("phase_end", name=name, seconds=time.monotonic() - started)


def _without_closed_links(new):
    """new less the postings whose apply link a check just found closed. The check
    waits up to LINK_WAIT_SECONDS for one the app started."""
    found = fetch.check_links(ids=[job["id"] for job in new], wait=LINK_WAIT_SECONDS)
    if not found:
        return new
    events.emit("info", text=f"[links] checked {found['checked']} new, {found['closed']} "
                             "closed and left out of ranking")
    closed = set(found["closed_ids"])
    return [job for job in new if job["id"] not in closed]


def _pipeline(run_id, opts):
    started = time.monotonic()
    # a fresh home has every company's icon to look up, a minute or more of work
    # that a first run leaves until its postings are ranked
    with _phase("fetch"):
        new, counts = fetch.fetch_new(dry_run=opts.dry_run, icons=not opts.first_run)

    if opts.limit is not None:
        held = len(new) - opts.limit
        new = new[:opts.limit]
        if held > 0:
            events.emit("info", text=f"--limit {opts.limit}: {held} posting(s) held back, "
                                     "still unseen.")

    new = _without_closed_links(new)

    if opts.dry_run:
        return RunResult(run_id=run_id, status="dry-run", counts=counts, results=new,
                         elapsed=time.monotonic() - started)

    with _phase("rank"):
        results, unranked = _ranked(experience.within_limit(new), run_id)
        experience.read_ranked()
    store.claim_check_scores(run_id)
    if opts.first_run:
        fetch.fetch_icons_later()

    follow_ups = _follow_ups()
    counts = {**counts, "ranked": len(results), "unranked": len(unranked),
              "summarised": sum(1 for r in results if r.get("keywords")),
              "follow_ups": len(follow_ups)}
    return RunResult(run_id=run_id, status="ok", counts=counts, results=results,
                     unranked=unranked, elapsed=time.monotonic() - started,
                     follow_ups=follow_ups)


def _ranked(new, run_id):
    """Rank new and commit the scores. Returns rank.process's (results, unranked)."""
    results, unranked = rank.process(new, run_id=run_id)
    applications.annotate(results)
    # a posting's score is its group's, so the members ranking skipped get it too
    scored = results + [{**r, "id": other} for r in results for other in r.get("also_ids", ())]
    store.save_scores(scored, run_id)
    # the one commit point. Only ranked postings are marked seen, after their scores
    # are stored, so whatever --limit, the rank cap or a failed batch dropped stays
    # unseen and retries
    store.mark_seen(r.get("id") for r in scored)
    return results, unranked


def _follow_ups():
    """The applications waiting on a follow-up, announced when there are any. The
    run has committed by now, so a failure here is a warning and no follow-ups."""
    try:
        stale = tracking.stale_applications()
    except Exception as e:  # noqa: BLE001 - the ranked postings are already stored
        events.emit("warn", text=f"[follow-ups] could not be listed: "
                                 f"{type(e).__name__}: {e}")
        return []
    if stale:
        events.emit("info", text=f"[follow-ups] {tracking.follow_up_note(stale)}")
    return stale
