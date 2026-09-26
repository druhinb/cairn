"""Routes for the home's counts, company icons, the doctor, and the daily schedule."""
import re
import time
from typing import Annotated

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, StringConstraints

from cairn import __version__, sources, store
from cairn.core import events, paths, settings
from cairn.jobs import logos
from cairn.server import system
from cairn.server.background import launch
from cairn.system import doctor, schedule

router = APIRouter()

WANTED_WAIT = 5  # seconds a batch of shown companies waits for a restore or icon fetch

LOGO_DOMAIN = re.compile(r"[a-z0-9-]+(\.[a-z0-9-]+)+")
MAX_DOMAIN_CHARS = 253
# a stored icon may be an SVG, which must not run script or load anything
LOGO_HEADERS = {"Cache-Control": "max-age=86400",
                "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'",
                "X-Content-Type-Options": "nosniff"}


class ScheduleBody(BaseModel):
    hour: int = Field(schedule.DEFAULT_HOUR, ge=0, le=23)
    minute: int = Field(schedule.DEFAULT_MINUTE, ge=0, le=59)


class WantedBody(BaseModel):
    companies: list[Annotated[str, StringConstraints(max_length=200)]] = Field(max_length=200)


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


def fetch_wanted(wanted):
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


@router.get("/api/status")
def status():
    return {**store.counts(), "home": str(paths.home()),
            "database": str(paths.db_file()), "version": __version__,
            "kinds": list(sources.KINDS),
            "icons": {**store.icon_counts(), "running": logos.icons_running()}}


@router.post("/api/icons", status_code=202)
def start_icons():
    if not settings.get().company_icons:
        raise HTTPException(409, "company icons are off in Settings")
    try:
        launch(_fetch_icons, on_failure=_icons_failed)
    except logos.IconsRunning as e:
        raise HTTPException(409, str(e)) from None
    return {"started": True}


@router.post("/api/icons/wanted", status_code=202)
def want_icons(body: WantedBody, request: Request):
    if not settings.get().company_icons:
        raise HTTPException(409, "company icons are off in Settings")
    return {"queued": request.app.state.wanted.add(body.companies)}


@router.get("/api/logos/{domain}")
def get_logo(domain: str):
    path = None
    if len(domain) <= MAX_DOMAIN_CHARS and LOGO_DOMAIN.fullmatch(domain):
        path = logos.logo_file(domain)
    if path is None:
        raise HTTPException(404, f"no logo stored for {domain}")
    return FileResponse(path, media_type=logos.content_type(path), headers=LOGO_HEADERS)


@router.post("/api/doctor")
def run_doctor():
    return {"checks": doctor.checks()}


@router.get("/api/schedule")
def get_schedule():
    return _schedule_call(schedule.status)


@router.post("/api/schedule/install")
def install_schedule(body: ScheduleBody = ScheduleBody()):
    return _schedule_call(lambda: schedule.install(body.hour, body.minute))


@router.post("/api/schedule/remove")
def remove_schedule():
    return _schedule_call(schedule.remove)
