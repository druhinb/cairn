"""Routes for application tracking: interview stages, checklists, files, follow-ups,
the Today summary, and the calendar feed."""
from datetime import datetime

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel

from cairn import store, tracking
from cairn.core import events
from cairn.server.rows import make_row

router = APIRouter()


class StageBody(BaseModel):
    stage: str
    at: str
    note: str | None = None


class StageChange(BaseModel):
    stage: str | None = None
    at: str | None = None
    note: str | None = None


class ItemBody(BaseModel):
    label: str


class ItemChange(BaseModel):
    label: str | None = None
    done: bool | None = None


class OrderBody(BaseModel):
    ids: list[int]


class AttachmentBody(BaseModel):
    label: str | None = None
    path: str


def _posting_or_404(posting_id):
    if not store.get_posting(posting_id):
        raise HTTPException(404, f"unknown posting id: {posting_id}")


def _found(value, what):
    if value is None:
        raise HTTPException(404, f"unknown {what}")
    return value


def _checked(status, call, *args):
    """call(*args), with its ValueError answered by status."""
    try:
        return call(*args)
    except ValueError as e:
        raise HTTPException(status, str(e)) from None


def _changed(posting_id):
    events.emit("tracking_changed", id=posting_id)


@router.post("/api/jobs/{posting_id}/stages")
def add_stage(posting_id: str, body: StageBody):
    _posting_or_404(posting_id)
    had_status = store.application(posting_id) is not None
    event = _checked(400, store.add_stage, posting_id, body.stage, body.at, body.note)
    if not had_status:
        events.emit("application_changed", id=posting_id,
                    status=store.application(posting_id)["status"])
    _changed(posting_id)
    return event


@router.patch("/api/stages/{event_id}")
def update_stage(event_id: int, body: StageChange):
    _found(store.get_stage(event_id), "stage")
    event = _checked(400, store.update_stage, event_id, body.stage, body.at, body.note)
    _changed(event["posting_id"])
    return event


@router.delete("/api/stages/{event_id}")
def delete_stage(event_id: int):
    _changed(_found(store.delete_stage(event_id), "stage"))
    return {"deleted": True}


@router.get("/api/jobs/{posting_id}/checklist")
def checklist(posting_id: str):
    _posting_or_404(posting_id)
    return store.checklist(posting_id)


@router.post("/api/jobs/{posting_id}/checklist")
def add_item(posting_id: str, body: ItemBody):
    _posting_or_404(posting_id)
    item = _checked(422, store.add_item, posting_id, body.label)
    _changed(posting_id)
    return item


@router.patch("/api/checklist/{item_id}")
def set_item(item_id: int, body: ItemChange):
    _found(store.checklist_item(item_id), "checklist item")
    item = _checked(422, store.set_item, item_id, body.label, body.done)
    _changed(item["posting_id"])
    return item


@router.delete("/api/checklist/{item_id}")
def delete_item(item_id: int):
    _changed(_found(store.delete_item(item_id), "checklist item"))
    return {"deleted": True}


@router.put("/api/jobs/{posting_id}/checklist/order")
def reorder(posting_id: str, body: OrderBody):
    _posting_or_404(posting_id)
    items = _checked(422, store.reorder, posting_id, body.ids)
    _changed(posting_id)
    return items


@router.get("/api/jobs/{posting_id}/attachments")
def attachments(posting_id: str):
    _posting_or_404(posting_id)
    return store.attachments(posting_id)


@router.post("/api/jobs/{posting_id}/attachments")
def add_attachment(posting_id: str, body: AttachmentBody):
    _posting_or_404(posting_id)
    attachment = _checked(400, store.add_attachment, posting_id, body.label, body.path)
    _changed(posting_id)
    return attachment


@router.delete("/api/attachments/{attachment_id}")
def delete_attachment(attachment_id: int):
    _changed(_found(store.delete_attachment(attachment_id), "file"))
    return {"deleted": True}


@router.get("/api/tracking/follow-ups")
def follow_ups():
    return tracking.stale_applications()


@router.get("/api/today")
def today(since: datetime | None = None):
    try:
        since = since and since.astimezone().replace(tzinfo=None)
    except OverflowError:
        raise HTTPException(400, "since: out of range") from None
    summary = tracking.today(since)
    return {**summary, "picks": [make_row(job) for job in summary["picks"]]}


@router.get("/api/calendar.ics")
def calendar(days: int = Query(tracking.CALENDAR_DAYS_AHEAD, ge=1, le=365)):
    return Response(tracking.ics(days), media_type="text/calendar; charset=utf-8",
                    headers={"Content-Disposition": 'attachment; filename="cairn.ics"'})
