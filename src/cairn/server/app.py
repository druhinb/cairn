"""The JSON API behind the native window, plus the static page that talks to it.

Handlers validate input and call store, pipeline, and settings; nothing else lives
here. Every error body is {"error": message}. LocalOnly guards every request: only
this Mac, addressed by a loopback name, from the app's own origin, gets an answer.
Every API request needs the session cookie that the launch URL's token buys, and a
change also needs the header only the app's page sends.
"""
import ast
import asyncio
import contextlib
import dataclasses
import email.message
import email.parser
import email.utils
import hmac
import http.cookies
import ipaddress
import json
import mimetypes
import os
import queue
import re
import tempfile
import threading
import time
from concurrent.futures import Future
from pathlib import Path
from secrets import token_urlsafe
from typing import Annotated, Literal
from urllib.parse import parse_qs, urlsplit

from fastapi import Body, FastAPI, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.exceptions import RequestValidationError
from fastapi.responses import (FileResponse, HTMLResponse, JSONResponse, PlainTextResponse,
                               Response, StreamingResponse)
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator
from starlette.exceptions import HTTPException as StarletteHTTPException

from cairn import __version__, sources, store
from cairn.ai import llm, onboard
from cairn.core import events, paths, settings
from cairn.jobs import descriptions, fetch, logos, pipeline, rank
from cairn.system import backup, doctor, schedule
from cairn.server import insights as insights_routes
from cairn.server import llm as llm_routes
from cairn.server import system
from cairn.server import tracking as tracking_routes
from cairn.server.rows import make_row

STATIC = Path(__file__).parent / "static"
# Windows takes these from the registry, which can call .js text/plain, and nosniff
# then blocks every module script
for _ext, _kind in ((".js", "text/javascript"), (".css", "text/css"), (".html", "text/html"),
                    (".svg", "image/svg+xml"), (".woff2", "font/woff2"), (".txt", "text/plain")):
    mimetypes.add_type(_kind, _ext)
KEEPALIVE = 15.0  # seconds of silence before the event stream sends a comment
POLL = 0.2  # seconds between event queue checks
START_TIMEOUT = 10  # seconds a background job gets to take the run lock
WANTED_WAIT = 5  # seconds a batch of shown companies waits for a restore or icon fetch
MAX_RESUME_BYTES = 10 * 1024 * 1024
MAX_RUN_LOG_BYTES = 4 * 2**20  # the most of run.log one request reads
LOGO_DOMAIN = re.compile(r"[a-z0-9-]+(\.[a-z0-9-]+)+")
MAX_DOMAIN_CHARS = 253
# a stored icon may be an SVG, which must not run script or load anything
LOGO_HEADERS = {"Cache-Control": "max-age=86400",
                "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'",
                "X-Content-Type-Options": "nosniff"}

# the favicon takes its light and dark colours from an inline <style>
FAVICON_CSP = "default-src 'none'; style-src 'unsafe-inline'"

EDITABLE = {"profile": paths.profile_md}

LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "[::1]"})
# one per process; page_url() hands it to whatever launched the process
SESSION = token_urlsafe(32)
TOKEN_PARAM = "t"
# lib/api.js sends it with every request; no form, and no request another site can
# send without a CORS preflight, carries it
APP_HEADER = "x-cairn"
MUTATING = frozenset({"POST", "PUT", "PATCH", "DELETE"})
PAGES = frozenset({"/", "/index.html"})
# the long-lived event stream, and the restore that waits for the other requests
UNCOUNTED = frozenset({"/api/run/events", "/api/restore"})
SECURITY_HEADERS = (
    (b"content-security-policy", (b"default-src 'self'; "
                                  b"object-src 'none'; base-uri 'none'; form-action 'self'; "
                                  b"frame-ancestors 'none'")),
    (b"x-frame-options", b"DENY"),
    (b"x-content-type-options", b"nosniff"),
    (b"referrer-policy", b"no-referrer"))
LOCKED_PAGE = """<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><meta name="color-scheme" content="light dark">
<link rel="icon" href="/favicon.svg" type="image/svg+xml"><title>Cairn</title></head>
<body>
<h1>Open Cairn from the app</h1>
<p>This page works only inside Cairn. Open the Cairn app to continue.</p>
</body>
</html>
"""

Status = Literal[store.STATUSES]


class ApplicationBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Status | None = None
    note: str | None = None

    @model_validator(mode="after")
    def _status_or_note(self):
        if self.status is None and self.note is None:
            raise ValueError("give a status, a note, or both")
        return self


class RunBody(BaseModel):
    limit: int | None = Field(None, ge=1)
    fit: int | None = None
    dry_run: bool = False
    first_run: bool = False


class FileBody(BaseModel):
    text: str


class OnboardApplyBody(BaseModel):
    profile_md: str
    prefs: dict = {}


class ResolveBody(BaseModel):
    query: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1,
                                            max_length=200)]


class ScheduleBody(BaseModel):
    hour: int = Field(schedule.DEFAULT_HOUR, ge=0, le=23)
    minute: int = Field(schedule.DEFAULT_MINUTE, ge=0, le=59)


class WantedBody(BaseModel):
    companies: list[Annotated[str, StringConstraints(max_length=200)]] = Field(max_length=200)


class RevalidatedStaticFiles(StaticFiles):
    """Static files the browser must revalidate on every load.

    Without it a browser, and the native window's WebView, keeps running the old
    scripts and stylesheets after the package is upgraded until a hard reload.
    """

    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache"
        if path == "favicon.svg":
            response.headers["Content-Security-Policy"] = FAVICON_CSP
        return response


def _error(status, message):
    return JSONResponse({"error": message}, status_code=status)


def _local_host(value):
    """Whether a Host header, or an Origin's netloc, names a loopback host, any port."""
    value = value.lower()
    if value.startswith("["):
        host, _, rest = value.partition("]")
        if rest and not rest.startswith(":"):
            return False
        host, port = host + "]", rest.removeprefix(":")
    else:
        host, _, port = value.partition(":")
    return host in LOCAL_HOSTS and (port == "" or port.isdigit())


def _loopback_client(scope):
    client = scope.get("client")
    try:
        address = client and ipaddress.ip_address(client[0])
    except ValueError:
        return False
    # Python before 3.11.10 reads ::ffff:127.0.0.1 as not loopback
    return bool(address) and (getattr(address, "ipv4_mapped", None) or address).is_loopback


def session_cookie(port):
    """The session cookie's name for the server on port. A browser keeps one cookie
    per host and name whatever the port, so each port's server needs its own name."""
    return f"cairn_session_{port}"


def _has_session(scope, headers):
    jar = http.cookies.SimpleCookie()
    try:
        jar.load(headers.get("cookie", ""))
    except http.cookies.CookieError:
        return False
    morsel = jar.get(session_cookie(scope["server"][1]))
    return morsel is not None and hmac.compare_digest(morsel.value.encode(), SESSION.encode())


def _has_launch_token(scope):
    tokens = parse_qs(scope["query_string"].decode("latin-1")).get(TOKEN_PARAM, [])
    return any(hmac.compare_digest(token.encode(), SESSION.encode()) for token in tokens)


def own_headers(url):
    """The headers that take a client in this process, the window's readiness check
    or the menu bar, past LocalOnly on the server at url."""
    return {"Cookie": f"{session_cookie(urlsplit(url).port)}={SESSION}", APP_HEADER: "1"}


def _refusal(scope, headers):
    """(status, message) when a request must not reach the app, else None."""
    if not _loopback_client(scope):
        return 403, "this server answers only requests from this computer"
    host = headers.get("host", "")
    if not _local_host(host):
        return 421, "this server answers only requests to 127.0.0.1, localhost, or [::1]"
    origin = headers.get("origin")
    # a page on another local port is same-site, and cookies ignore the port
    if (origin is not None and origin.lower() != f"http://{host.lower()}") \
            or headers.get("sec-fetch-site") in ("cross-site", "same-site"):
        return 403, "requests from other websites are refused"
    if scope["path"].startswith("/api/"):
        if not _has_session(scope, headers):
            return 403, "this page lost its link to Cairn. Close it and open Cairn again"
        if scope["method"] in MUTATING and APP_HEADER not in headers:
            return 403, "a change must come from the app's own page"
    return None


def _page_answer(scope, headers):
    """The answer to a request for the page, when it must not get the page itself: a
    launch URL trades its token for the session cookie, and a request with neither
    gets LOCKED_PAGE."""
    if _has_launch_token(scope):
        cookie = (f"{session_cookie(scope['server'][1])}={SESSION}; HttpOnly; "
                  f"SameSite=Strict; Path=/")
        return Response(status_code=303, headers={"location": "/", "set-cookie": cookie})
    if _has_session(scope, headers):
        return None
    return HTMLResponse(LOCKED_PAGE, status_code=403)


class LocalOnly:
    """ASGI middleware: refuse a request from another machine, host, or website, and
    an API request without the session; trade a launch URL's token for the session
    cookie; answer 503 to API requests while a restore runs. Every response gets
    SECURITY_HEADERS.

    A plain ASGI class, since BaseHTTPMiddleware would buffer the event stream.
    """

    def __init__(self, app, maintenance):
        self.app, self.maintenance = app, maintenance

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        path = scope["path"]
        send = _with_security_headers(send, api=path.startswith("/api/"))
        headers = {k.decode("latin-1"): v.decode("latin-1") for k, v in scope["headers"]}
        refusal = _refusal(scope, headers)
        if refusal:
            return await _error(*refusal)(scope, receive, send)
        if path in PAGES and scope["method"] in ("GET", "HEAD"):
            answer = _page_answer(scope, headers)
            if answer is not None:
                return await answer(scope, receive, send)
        if not path.startswith("/api/") or path in UNCOUNTED:
            return await self.app(scope, receive, send)
        if not self.maintenance.enter():
            return await _error(503, "Cairn is restoring a backup. Try again in a moment")(
                scope, receive, send)
        try:
            await self.app(scope, receive, send)
        finally:
            self.maintenance.leave()


def _with_security_headers(send, api):
    """send, adding each of SECURITY_HEADERS a response does not set itself, and
    Cache-Control: no-store to an API response that sets none."""
    added = (*SECURITY_HEADERS, *([(b"cache-control", b"no-store")] if api else []))

    async def sending(message):
        if message["type"] == "http.response.start":
            own = message.get("headers", [])
            named = {name.lower() for name, _ in own}
            message = {**message, "headers": [
                *own, *((name, value) for name, value in added if name not in named)]}
        await send(message)
    return sending


def _relevant(posting_id):
    where, params = store.relevant_query(settings.get())
    return store.connect().execute(
        f"SELECT 1 FROM postings WHERE postings.id = ? AND {where}",
        [posting_id, *params]).fetchone() is not None


def _detail(job):
    description = store.get_description(job["id"])
    return {**make_row(job), "relevant": _relevant(job["id"]),
            "terms": job["terms"], "degrees": job["degrees"],
            "feed_sponsorship": job["sponsorship"], "active": job["active"],
            "gap": descriptions.gap(job.get("keywords")),
            "posting_updated_at": job["date_updated"], "first_seen_at": job["first_seen_at"],
            "last_seen_at": job["last_seen_at"], "keywords": job.get("keywords"),
            "events": store.application_events(job["id"]),
            "description": description and {k: description[k] for k in
                                            ("keywords", "fetched_at", "error")}}


def _posting_or_404(posting_id):
    job = store.get_posting(posting_id)
    if not job:
        raise HTTPException(404, f"unknown posting id: {posting_id}")
    return job


def _launch(work, on_failure=None):
    """Run work(started) on a daemon thread. Returns what it passes to started, if anything.

    Whatever work raises before calling started is raised here, which is how a held
    run lock reaches the request as a 409. A later failure goes to on_failure(error)
    and nowhere else.
    """
    ready = Future()

    def body():
        try:
            work(lambda value=None: ready.set_result(value))
        except Exception as e:  # noqa: BLE001 - reported to the request or on_failure
            if not ready.done():
                ready.set_exception(e)
            elif on_failure:
                on_failure(e)
        finally:
            store.close_thread()
            system.maintenance.leave()
            if not ready.done():
                ready.set_exception(RuntimeError("the job finished without starting"))

    # a restore waits for the job, which outlives the request that started it
    system.maintenance.hold()
    threading.Thread(target=body, daemon=True).start()
    try:
        return ready.result(timeout=START_TIMEOUT)
    except TimeoutError:
        raise HTTPException(500, "background job did not start") from None


def _write_atomic(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent,
                                     prefix=f".{path.name}.", delete=False) as f:
        f.write(text)
    os.replace(f.name, path)


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


def _add_error_handlers(app):
    @app.exception_handler(StarletteHTTPException)
    async def http_error(request, exc):
        return _error(exc.status_code, str(exc.detail))

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request, exc):
        if any(e["type"] == "json_invalid" for e in exc.errors()):
            return _error(400, "request body is not valid JSON")
        problems = ["{}: {}".format(".".join(str(p) for p in e["loc"][1:]) or "body",
                                    e["msg"]) for e in exc.errors()]
        return _error(400, "; ".join(problems))

    @app.exception_handler(pipeline.RunInProgress)
    async def run_in_progress(request, exc):
        return _error(409, str(exc))

    @app.exception_handler(onboard.OnboardError)
    async def bad_onboarding(request, exc):
        return _error(400, str(exc))

    @app.exception_handler(onboard.ClaudeFailed)
    async def claude_failed(request, exc):
        return _error(502, str(exc))

    @app.exception_handler(onboard.FilesExist)
    async def files_exist(request, exc):
        return JSONResponse({"error": str(exc), "files": [str(p) for p in exc.paths]},
                            status_code=409)

    @app.exception_handler(settings.SettingsError)
    async def bad_settings(request, exc):
        return _error(400, str(exc))

    @app.exception_handler(Exception)
    async def unexpected(request, exc):
        return _error(500, f"{type(exc).__name__}: {exc}")


def _add_job_routes(app):
    @app.get("/api/jobs")
    def list_jobs(q: str | None = None, relevant_only: bool = True,
                  fit_min: int | None = None, tier_min: int | None = None,
                  status: list[Literal[(*store.STATUSES, "none")]] | None = Query(None),
                  hide_passed: bool = True,
                  category: list[str] | None = Query(None), location: str | None = None,
                  source: list[str] | None = Query(None),
                  posted_within_days: float | None = None,
                  active_only: bool = True, run_id: int | None = None,
                  sponsorship: Literal["yes", "no"] | None = None,
                  salary_min: int | None = Query(None, ge=0),
                  sort: Literal["score", "newest", "company", "updated", "salary"] = "score",
                  limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0)):
        rows, total = store.search(
            q=q, relevant_only=relevant_only, fit_min=fit_min, tier_min=tier_min,
            status=status, hide_passed=hide_passed, category=category, location=location,
            source=source, posted_within_days=posted_within_days,
            active_only=active_only, run_id=run_id, sponsorship=sponsorship,
            salary_min=salary_min, sort=sort, limit=limit, offset=offset)
        return {"rows": [make_row(job) for job in rows], "total": total}

    @app.get("/api/jobs/{posting_id}")
    def get_job(posting_id: str):
        return _detail(_posting_or_404(posting_id))

    @app.put("/api/jobs/{posting_id}/application")
    def put_application(posting_id: str, body: ApplicationBody):
        _posting_or_404(posting_id)
        if body.status is not None:
            store.set_status(posting_id, body.status, body.note)
        elif store.set_note(posting_id, body.note) is None:
            raise HTTPException(409, "the posting has no status to attach a note to")
        job = store.get_posting(posting_id)
        events.emit("application_changed", id=posting_id, status=job["status"])
        return _detail(job)

    @app.delete("/api/jobs/{posting_id}/application")
    def clear_application(posting_id: str):
        _posting_or_404(posting_id)
        store.clear_application(posting_id)
        events.emit("application_changed", id=posting_id, status=None)
        events.emit("tracking_changed", id=posting_id)
        return _detail(store.get_posting(posting_id))

    @app.get("/api/applications")
    def list_applications(status: list[Status] | None = Query(None)):
        return {"rows": [{**row, "url": store.web_url(row["url"])}
                         for row in store.applications(status)],
                "counts": store.application_counts()}

    @app.post("/api/jobs/{posting_id}/summary", status_code=202)
    def summarise_job(posting_id: str, force: bool = False):
        _posting_or_404(posting_id)

        def work(started):
            started()
            pipeline.summarise_posting(posting_id, force=force)

        def failed(e):
            events.emit("warn",
                        text=f"summarising {posting_id} failed: {type(e).__name__}: {e}")

        _launch(work, on_failure=failed)
        return {"started": True}

    @app.post("/api/jobs/{posting_id}/reasons")
    def explain_scores(posting_id: str):
        job = _posting_or_404(posting_id)
        if job.get("fit") is None:
            raise HTTPException(409, "this posting has no score to explain")
        if not llm_routes.configured(llm.active()):
            raise HTTPException(409, "no AI provider is set up. Choose one in Settings")
        if not paths.profile_md().exists():
            raise HTTPException(409, "there's no profile yet. Finish setup first")
        try:
            rank.explain(job)
        except rank.CapReached as e:
            raise HTTPException(429, f"you've used your limit of {e.cap} AI requests for "
                                     "the month. Raise Monthly AI limit in Settings › "
                                     "Ranking, or wait a few days") from None
        except rank.ExplainFailed as e:
            raise HTTPException(502, str(e)) from None
        events.emit("scores_explained", id=posting_id)
        return _detail(store.get_posting(posting_id))


def _add_settings_routes(app):
    @app.get("/api/settings")
    def get_settings():
        return {**dataclasses.asdict(settings.base()),
                "defaults": dataclasses.asdict(settings.defaults()),
                "path": str(paths.config_file())}

    @app.put("/api/settings")
    def put_settings(body: dict = Body(...)):
        new = settings.from_dict(body, base=settings.base(), source="request")
        settings.save(new)
        settings.use(new)
        store.sync_relevance()
        return dataclasses.asdict(new)

    @app.get("/api/files/{name}")
    def get_file(name: str):
        if name not in EDITABLE:
            raise HTTPException(404, f"no such file: {name}")
        path = EDITABLE[name]()
        text = path.read_text(encoding="utf-8") if path.exists() else ""
        return {"name": name, "path": str(path), "text": text}

    @app.put("/api/files/{name}")
    def put_file(name: str, body: FileBody):
        if name not in EDITABLE:
            raise HTTPException(404, f"no such file: {name}")
        path = EDITABLE[name]()
        _write_atomic(path, body.text)
        return {"name": name, "path": str(path), "text": body.text}

    @app.post("/api/watchlist/resolve")
    def resolve_watchlist(body: ResolveBody):
        found = sources.resolve_company(body.query)
        if found is None:
            raise HTTPException(404, f"Cairn couldn't find a careers page for {body.query}")
        return {"kind": found.kind, "location": found.location, "company": found.company}


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


def _add_run_routes(app):
    @app.post("/api/run", status_code=202)
    def start_run(body: RunBody = RunBody()):
        opts = pipeline.RunOptions(**body.model_dump())
        run_id = _launch(lambda started: pipeline.run(opts, on_start=started))
        return {"run_id": run_id}

    @app.get("/api/run/events")
    async def run_events(request: Request):
        return StreamingResponse(_event_stream(request), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-store"})

    @app.get("/api/run/status")
    def run_status():
        pid = pipeline.lock_holder()
        return {"running": pid is not None, "icons": logos.icons_running(), "pid": pid,
                "run": None if pid is None else _running_run()}

    @app.get("/api/runs")
    def list_runs(limit: int = Query(20, ge=1, le=200)):
        runs = store.list_runs(limit)
        relevant = store.relevant_ranked(settings.get(), [run["id"] for run in runs])
        return [{**run, "relevant_ranked": relevant.get(run["id"], 0)} for run in runs]

    @app.get("/api/runs/{run_id}")
    def get_run(run_id: int):
        return _run_or_404(run_id)

    @app.get("/api/runs/{run_id}/log", response_class=PlainTextResponse)
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


def _schedule_call(action):
    try:
        return action()
    except schedule.ScheduleError as e:
        raise HTTPException(500, str(e)) from None


def _fetch_icons(started):
    """The work of a POST /api/icons, fetch_all holding the icon marker. Reports each
    pass as icons_progress and the end as icons_done."""
    def progress(done, remaining):
        events.emit("icons_progress", done=done, remaining=remaining)

    with logos.icon_job():
        started()
        job = logos.fetch_all(progress)
    events.emit("icons_done", fetched=job.fetched,
                error=logos.OFFLINE if job.offline else None)


def _icons_failed(e):
    error = f"{type(e).__name__}: {e}"
    events.emit("warn", text=f"fetching company icons failed: {error}")
    events.emit("icons_done", fetched=None, error=error)


def _fetch_wanted(wanted):
    """Fetch a batch of a logos.Wanted's companies holding the icon marker, and report
    their icons as icons_found. While a restore runs or another icon fetch holds the
    marker, the batch waits WANTED_WAIT seconds and stays queued."""
    icons = None
    if system.maintenance.enter():
        try:
            with logos.icon_job():
                icons = logos.fetch_wanted(wanted.take())
        except logos.IconsRunning:
            icons = None
        finally:
            system.maintenance.leave()
    if icons is None:
        time.sleep(WANTED_WAIT)
    elif icons:
        events.emit("icons_found", icons=icons)


def _add_status_routes(app):
    @app.get("/api/status")
    def status():
        return {**store.counts(), "home": str(paths.home()),
                "database": str(paths.db_file()), "version": __version__,
                "kinds": list(sources.KINDS),
                "icons": {**store.icon_counts(), "running": logos.icons_running()}}

    @app.post("/api/icons", status_code=202)
    def start_icons():
        if not settings.get().company_icons:
            raise HTTPException(409, "company icons are off in Settings")
        try:
            _launch(_fetch_icons, on_failure=_icons_failed)
        except logos.IconsRunning as e:
            raise HTTPException(409, str(e)) from None
        return {"started": True}

    wanted = logos.Wanted(_fetch_wanted)

    @app.post("/api/icons/wanted", status_code=202)
    def want_icons(body: WantedBody):
        if not settings.get().company_icons:
            raise HTTPException(409, "company icons are off in Settings")
        return {"queued": wanted.add(body.companies)}

    @app.get("/api/logos/{domain}")
    def get_logo(domain: str):
        path = None
        if len(domain) <= MAX_DOMAIN_CHARS and LOGO_DOMAIN.fullmatch(domain):
            path = logos.logo_file(domain)
        if path is None:
            raise HTTPException(404, f"no logo stored for {domain}")
        return FileResponse(path, media_type=logos.content_type(path), headers=LOGO_HEADERS)

    @app.post("/api/doctor")
    def run_doctor():
        return {"checks": doctor.checks()}

    @app.get("/api/schedule")
    def get_schedule():
        return _schedule_call(schedule.status)

    @app.post("/api/schedule/install")
    def install_schedule(body: ScheduleBody = ScheduleBody()):
        return _schedule_call(lambda: schedule.install(body.hour, body.minute))

    @app.post("/api/schedule/remove")
    def remove_schedule():
        return _schedule_call(schedule.remove)


def _form_file(body, content_type, field):
    """(filename, bytes) of one file field in a multipart/form-data body, or None."""
    header = email.message.EmailMessage()
    header["content-type"] = content_type
    boundary = header.get_boundary()
    if not boundary:
        raise HTTPException(400, "multipart body has no boundary")
    for part in body.split(b"--" + boundary.encode())[1:-1]:
        head, _, content = part.lstrip(b"\r\n").partition(b"\r\n\r\n")
        # compat32 keeps a backslash in a quoted file name, which the HTTP policy drops
        headers = email.parser.BytesHeaderParser().parsebytes(head)
        if headers.get_param("name", header="content-disposition") == field:
            filename = headers.get_param("filename", "", header="content-disposition")
            return (email.utils.collapse_rfc2231_value(filename),
                    content.removesuffix(b"\r\n"))
    return None


async def _capped_body(request):
    """The request body, refused with 413 once it passes MAX_RESUME_BYTES."""
    too_big = f"the resume is over {MAX_RESUME_BYTES // 2**20} MB"
    declared = request.headers.get("content-length")
    if declared is not None:
        if not declared.isdigit():
            raise HTTPException(400, "content-length is not a number")
        if int(declared) > MAX_RESUME_BYTES:
            raise HTTPException(413, too_big)
    chunks, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_RESUME_BYTES:
            raise HTTPException(413, too_big)
        chunks.append(chunk)
    return b"".join(chunks)


def _upload_suffix(filename):
    if "\0" in filename or "/" in filename or "\\" in filename:
        raise HTTPException(400, "the uploaded file name contains a path or NUL character")
    return Path(filename).suffix.lower() or ".txt"


async def _uploaded_resume(request):
    """(suffix, bytes, overwrite) from a JSON {"text", "overwrite"} body, or a multipart
    body with a `resume` file and an optional `overwrite` field of "true"."""
    body = await _capped_body(request)
    content_type = request.headers.get("content-type", "")
    if content_type.lower().startswith("multipart/form-data"):
        found = _form_file(body, content_type, "resume")
        if found is None:
            raise HTTPException(400, "upload the resume as the form field 'resume'")
        filename, data = found
        overwrite = _form_file(body, content_type, "overwrite")
        return _upload_suffix(filename), data, overwrite is not None and overwrite[1] == b"true"
    usage = 'expected {"text": "..."} or a multipart resume upload'
    try:
        payload = json.loads(body)
        text, overwrite = payload["text"], payload.get("overwrite", False)
    except (ValueError, TypeError, KeyError, AttributeError):
        raise HTTPException(400, usage) from None
    if not isinstance(text, str):
        raise HTTPException(400, "text: should be a string")
    if not isinstance(overwrite, bool):
        raise HTTPException(400, "overwrite: should be true or false")
    return ".txt", text.encode("utf-8"), overwrite


def _draft(suffix, data, overwrite):
    # refused before the 20-60 s Claude call, which apply would refuse anyway
    own = [] if overwrite else onboard.conflicts()
    if own:
        raise onboard.FilesExist(own)
    # pasted text goes through a file too, so text that names a path is never read
    with tempfile.TemporaryDirectory() as scratch:
        upload = Path(scratch) / f"resume{suffix}"
        upload.write_bytes(data)
        text = onboard.resume_text(upload)
    events.emit("info", text=f"[setup] read {len(text.split())} words from the resume")
    with pipeline.run_lock():
        draft = onboard.draft_from_resume(text)
    return dataclasses.asdict(draft)


def _add_onboard_routes(app):
    @app.get("/api/onboard/status")
    def onboard_status():
        return {"initialised": onboard.initialised(), "needs_setup": onboard.needs_setup()}

    @app.get("/api/onboard/options")
    def onboard_options():
        return {"roles": list(onboard.ROLE_KEYWORDS),
                "work_authorization": list(onboard.WORK_AUTHORIZATION)}

    @app.post("/api/onboard/draft")
    async def onboard_draft(request: Request):
        suffix, data, overwrite = await _uploaded_resume(request)
        return await run_in_threadpool(_draft, suffix, data, overwrite)

    @app.post("/api/onboard/apply")
    def onboard_apply(body: OnboardApplyBody):
        with pipeline.run_lock():
            applied = onboard.apply(onboard.Draft(body.profile_md, {}), body.prefs)
        return {"written": [str(path) for path in applied.written],
                "unresolved": applied.unresolved,
                "settings": dataclasses.asdict(applied.settings)}

    @app.post("/api/seed")
    def seed():
        with pipeline.run_lock():
            return {"seeded": fetch.seed()}


@contextlib.asynccontextmanager
async def _serving(app):
    with backup.serving():
        yield


def create_app():
    paths.lock_down()
    app = FastAPI(title="Cairn", lifespan=_serving)
    # an open event stream never ends by itself, so it ends when the server is stopping
    app.state.shutting_down = lambda: False
    app.state.maintenance = system.maintenance
    app.add_middleware(LocalOnly, maintenance=system.maintenance)
    _add_error_handlers(app)
    # /api/jobs/facets must register before /api/jobs/{posting_id}
    app.include_router(insights_routes.router)
    for add_routes in (_add_job_routes, _add_settings_routes, _add_run_routes,
                       _add_status_routes, _add_onboard_routes):
        add_routes(app)
    for module in (llm_routes, system, tracking_routes):
        app.include_router(module.router)
    # "/" matches every path, so the static mount goes after every API route
    app.mount("/", RevalidatedStaticFiles(directory=STATIC, html=True), name="static")
    return app
