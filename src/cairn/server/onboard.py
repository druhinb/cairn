"""Routes for first-run setup: the resume upload, the draft profile, and the seed."""
import dataclasses
import email.message
import email.parser
import email.utils
import json
import tempfile
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel

from cairn.ai import onboard
from cairn.core import events
from cairn.jobs import fetch, pipeline

router = APIRouter()

MAX_RESUME_BYTES = 10 * 1024 * 1024


class OnboardApplyBody(BaseModel):
    # None keeps profile.md and writes the answers into it
    profile_md: str | None = None
    prefs: dict = {}


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


@router.get("/api/onboard/status")
def onboard_status():
    return {"initialised": onboard.initialised(), "needs_setup": onboard.needs_setup()}


@router.get("/api/onboard/options")
def onboard_options():
    return {"roles": {role: {"keywords": keywords, "unskips": onboard.ROLE_UNSKIPS.get(role, [])}
                      for role, keywords in onboard.ROLE_KEYWORDS.items()},
            "skips": {group: {"list": title_list, "words": words}
                      for group, (title_list, words) in onboard.SKIP_GROUPS.items()},
            "work_authorization": list(onboard.WORK_AUTHORIZATION)}


@router.post("/api/onboard/draft")
async def onboard_draft(request: Request):
    suffix, data, overwrite = await _uploaded_resume(request)
    return await run_in_threadpool(_draft, suffix, data, overwrite)


@router.post("/api/onboard/apply")
def onboard_apply(body: OnboardApplyBody):
    with pipeline.run_lock():
        if body.profile_md is None:
            applied = onboard.answer_again(body.prefs)
        else:
            applied = onboard.apply(onboard.Draft(body.profile_md, {}), body.prefs)
    return {"written": [str(path) for path in applied.written],
            "unresolved": applied.unresolved,
            "settings": dataclasses.asdict(applied.settings)}


@router.post("/api/seed")
def seed():
    with pipeline.run_lock():
        return {"seeded": fetch.seed()}
