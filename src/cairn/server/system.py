"""Routes for the app's place on this computer: login item, updates, API keys, backup and
restore."""
import contextlib
import threading
import time

from fastapi import APIRouter, Body, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, PlainTextResponse
from starlette.datastructures import UploadFile

from cairn.ai import llm
from cairn.core import paths, secrets
from cairn.jobs import insights, logos, pipeline
from cairn.system import backup, schedule, update

router = APIRouter()

RESTORE_WAIT = 30  # seconds a restore waits for requests and background jobs to finish
IDLE_POLL = 0.1
# the multipart framing around the zip in an upload
FORM_OVERHEAD = 64 * 1024


def _uncounted_work():
    """Whether an icon fetch, here or in another process, or a link check runs."""
    return logos.icons_running() or insights.LINK_LOCK.locked()


class Maintenance:
    """Whether a restore is swapping the home's files, and the API requests and
    background jobs it waits for before it does."""

    def __init__(self):
        self._changed = threading.Condition()
        self.restoring = False
        self._busy = 0

    def enter(self):
        """Count one request in; False while a restore runs, which refuses it."""
        with self._changed:
            if self.restoring:
                return False
            self._busy += 1
            return True

    def hold(self):
        """Count one background job in, started by a request already counted."""
        with self._changed:
            self._busy += 1

    def leave(self):
        with self._changed:
            self._busy -= 1
            self._changed.notify_all()

    @contextlib.contextmanager
    def restore(self, timeout):
        """Refuse new requests, wait up to timeout seconds for the counted ones, an
        icon fetch, and a link check to end, and hold that state until the block exits."""
        with self._changed:
            if self.restoring:
                raise HTTPException(409, "a restore is already running")
            self.restoring = True
        try:
            deadline = time.monotonic() + timeout
            with self._changed:
                idle = self._changed.wait_for(lambda: self._busy == 0, timeout)
            while idle and _uncounted_work():
                idle = time.monotonic() < deadline
                time.sleep(IDLE_POLL)
            if not idle:
                raise HTTPException(409, "Cairn is busy. Try the restore again "
                                         "in a moment")
            yield
        finally:
            with self._changed:
                self.restoring = False


maintenance = Maintenance()


def _autostart_call(action):
    try:
        return action()
    except schedule.ScheduleError as e:
        raise HTTPException(500, str(e)) from None


@router.get("/api/autostart")
def get_autostart():
    return schedule.autostart_status()


@router.post("/api/autostart/install")
def install_autostart():
    return _autostart_call(schedule.autostart_install)


@router.post("/api/autostart/remove")
def remove_autostart():
    return _autostart_call(schedule.autostart_remove)


@router.get("/api/update")
def check_update():
    return update.check()


@router.post("/api/update")
def check_update_now():
    """Ask GitHub even when it was asked in the last day."""
    return update.check(force=True)


def _key_names():
    """Every key the app asks for: each provider that needs one, and USAJOBS."""
    return [pid for pid, spec in llm.PROVIDERS.items() if spec["needs_key"]] + [secrets.USAJOBS]


def _known_key(name):
    if name not in _key_names():
        raise HTTPException(400, f"no key named {name!r}, expected one of: {', '.join(_key_names())}")


@router.get("/api/keys")
def list_keys():
    """{name: whether a key is stored}; no response carries a key."""
    return {name: secrets.has_key(name) for name in _key_names()}


@router.put("/api/keys/{name}")
def put_key(name: str, body: dict = Body(...)):
    _known_key(name)
    unknown = sorted(set(body) - {"key"})
    if unknown:
        raise HTTPException(400, f"unknown field(s): {', '.join(unknown)}")
    key = body.get("key")
    if not isinstance(key, str):
        raise HTTPException(400, "key: should be a string")
    try:
        secrets.set_key(name, key)
    except secrets.SecretsError as e:
        raise HTTPException(400, f"key: {e}") from None
    return list_keys()


@router.delete("/api/keys/{name}")
def delete_key(name: str):
    _known_key(name)
    secrets.delete_key(name)
    return list_keys()


@router.post("/api/backup")
def make_backup():
    try:
        path = backup.export()
    except OSError as e:
        raise HTTPException(500, f"backup failed: {e}") from None
    return {"path": str(path), "bytes": path.stat().st_size}


@router.get("/api/settings/export", response_class=PlainTextResponse)
def export_settings():
    try:
        text = backup.export_settings()
    except FileNotFoundError:
        raise HTTPException(404, f"no config.toml in {paths.home()}") from None
    return PlainTextResponse(text, headers={
        "Content-Disposition": 'attachment; filename="config.toml"'})


def _restore(zip_file, accept_model_change):
    holder = pipeline.lock_holder()
    if holder is not None:
        raise pipeline.RunInProgress(holder)
    with maintenance.restore(RESTORE_WAIT):
        return backup.restore(zip_file, accept_model_change)


@router.post("/api/restore", status_code=202)
async def restore_backup(request: Request, accept_model_change: bool = False):
    """Swap the home's files for those in the uploaded form field `backup`. Every
    other API request answers 503 meanwhile. The store reopens its connections on the
    next request and the settings reload, so the server keeps running.

    A backup that changes backup.MODEL_SETTINGS is refused with 409 and the changes
    as model_changes until it is sent again with accept_model_change."""
    too_big = (f"this file is over {backup.MAX_ZIP // 2**30} GiB, too large to be a "
               "Cairn backup")
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > backup.MAX_ZIP + FORM_OVERHEAD:
        raise HTTPException(413, too_big)
    async with request.form(max_files=1, max_fields=0) as form:
        upload = form.get("backup")
        if not isinstance(upload, UploadFile):
            raise HTTPException(400, "upload the backup zip as the form field 'backup'")
        if upload.size is not None and upload.size > backup.MAX_ZIP:
            raise HTTPException(413, too_big)
        try:
            aside = await run_in_threadpool(_restore, upload.file, accept_model_change)
        except backup.ModelChange as e:
            return JSONResponse({"error": str(e), "model_changes": e.changes},
                                status_code=409)
        except backup.BackupError as e:
            raise HTTPException(400, str(e)) from None
        except pipeline.RunInProgress as e:
            raise HTTPException(409, str(e)) from None
    return {"previous": str(aside), "restart_required": False}
