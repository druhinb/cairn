"""Routes for runs: starting one, its live events, and past runs and their logs."""
import ast
import asyncio
import json
import os
import queue
import time

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse, StreamingResponse
from pydantic import BaseModel, Field

from cairn import store
from cairn.core import events, paths, settings
from cairn.jobs import logos, pipeline
from cairn.server.background import launch

router = APIRouter()

KEEPALIVE = 15.0  # seconds of silence before the event stream sends a comment
POLL = 0.2  # seconds between event queue checks

MAX_RUN_LOG_BYTES = 4 * 2**20  # the most of run.log one request reads


class RunBody(BaseModel):
    limit: int | None = Field(None, ge=1)
    fit: int | None = None
    dry_run: bool = False
    first_run: bool = False


def _sse(event):
    data = json.dumps({"at": event.at, **event.data}, default=str)
    return f"event: {event.kind}\ndata: {data}\n\n"


async def _event_stream(request):
    pending, unsubscribe = events.queue_subscriber()
    try:
        # the first bytes tell the client the stream is open
        yield ": keepalive\n\n"
        last_sent = time.monotonic()
        while not (request.app.state.shutting_down()
                   or await request.is_disconnected()):
            if pending.dropped:
                pending.dropped = False
                yield _sse(events.Event("warn", time.time(),
                                        {"text": "events dropped: client too slow"}))
            try:
                event = pending.get_nowait()
            except queue.Empty:
                if time.monotonic() - last_sent >= KEEPALIVE:
                    yield ": keepalive\n\n"
                    last_sent = time.monotonic()
                await asyncio.sleep(POLL)
                continue
            yield _sse(event)
            last_sent = time.monotonic()
    finally:
        unsubscribe()


def _run_or_404(run_id):
    run = store.get_run(run_id)
    if not run:
        raise HTTPException(404, f"no such run: {run_id}")
    return run


def _option(text):
    try:
        return ast.literal_eval(text)
    except (ValueError, SyntaxError):
        return text


def _run_start(run):
    """(byte offset, options) of the run's run_start line in its log, or None.

    A run's log range is recorded only when it ends, so an unfinished run is found
    by the line logfile writes for its run_start event, in the last
    MAX_RUN_LOG_BYTES of run.log.
    """
    try:
        with open(paths.run_log(), "rb") as f:
            offset = max(0, f.seek(0, os.SEEK_END) - MAX_RUN_LOG_BYTES)
            f.seek(offset)
            data = f.read(MAX_RUN_LOG_BYTES)
    except FileNotFoundError:
        return None
    marker = f"  run {run['id']} started  ".encode()
    at = data.rfind(marker)
    if at < 0:
        return None
    end = data.find(b"\n", at)
    rest = data[at + len(marker):None if end < 0 else end].decode("utf-8", errors="replace")
    pairs = (pair.split("=", 1) for pair in rest.split("  ") if "=" in pair)
    return (offset + data.rfind(b"\n", 0, at) + 1,
            {key: _option(value) for key, value in pairs})


def _running_run():
    """The newest run when it is still running: id, start time, and its options."""
    latest = store.list_runs(1)
    if not latest or latest[0]["status"] != "running":
        return None
    run = latest[0]
    start = _run_start(run)
    return {"id": run["id"], "started_at": run["started_at"],
            "options": start[1] if start else {}}


@router.post("/api/run", status_code=202)
def start_run(body: RunBody = RunBody()):
    opts = pipeline.RunOptions(**body.model_dump())
    run_id = launch(lambda started: pipeline.run(opts, on_start=started))
    return {"run_id": run_id}


@router.get("/api/run/events")
async def run_events(request: Request):
    return StreamingResponse(_event_stream(request), media_type="text/event-stream",
                             headers={"Cache-Control": "no-store"})


@router.get("/api/run/status")
def run_status():
    pid = pipeline.lock_holder()
    return {"running": pid is not None, "icons": logos.icons_running(), "pid": pid,
            "run": None if pid is None else _running_run()}


@router.get("/api/runs")
def list_runs(limit: int = Query(20, ge=1, le=200)):
    runs = store.list_runs(limit)
    relevant = store.relevant_ranked(settings.get(), [run["id"] for run in runs])
    return [{**run, "relevant_ranked": relevant.get(run["id"], 0)} for run in runs]


@router.get("/api/runs/{run_id}")
def get_run(run_id: int):
    return _run_or_404(run_id)


@router.get("/api/runs/{run_id}/log", response_class=PlainTextResponse)
def run_log(run_id: int):
    run = _run_or_404(run_id)
    start, end = run["log_start"], run["log_end"]
    if start is None and run["status"] == "running":
        found = _run_start(run)
        start = found[0] if found else None
    if start is None:
        return ""
    # run.log alone, whatever log_path a restored database names; a range longer
    # than MAX_RUN_LOG_BYTES is cut to its end, where a failure shows
    try:
        with open(paths.run_log(), "rb") as f:
            size = f.seek(0, os.SEEK_END)
            stop = size if end is None else min(end, size)
            start = max(start, stop - MAX_RUN_LOG_BYTES, 0)
            f.seek(start)
            return f.read(max(0, stop - start)).decode("utf-8", errors="replace")
    except FileNotFoundError:
        return ""
