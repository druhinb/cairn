"""Routes for score feedback, faceted counts, and the insights module."""
import threading
from typing import Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from cairn import store
from cairn.core import events, settings
from cairn.jobs import insights

router = APIRouter()


class FeedbackBody(BaseModel):
    verdict: str
    reason: str | None = None
    note: str | None = None


def _posting_or_404(posting_id):
    job = store.get_posting(posting_id)
    if not job:
        raise HTTPException(404, f"unknown posting id: {posting_id}")
    return job


@router.put("/api/jobs/{posting_id}/feedback")
def put_feedback(posting_id: str, body: FeedbackBody):
    _posting_or_404(posting_id)
    try:
        saved = store.set_feedback(posting_id, body.verdict, body.reason, body.note)
    except ValueError as e:
        raise HTTPException(422, str(e)) from None
    events.emit("feedback_changed", id=posting_id, verdict=body.verdict)
    return saved


@router.delete("/api/jobs/{posting_id}/feedback")
def delete_feedback(posting_id: str):
    _posting_or_404(posting_id)
    store.clear_feedback(posting_id)
    events.emit("feedback_changed", id=posting_id, verdict=None)
    return {"cleared": True}


@router.get("/api/feedback/stats")
def feedback_stats():
    return store.feedback_stats()


# must be included before the app's /api/jobs/{posting_id}, which would match it too
@router.get("/api/jobs/facets")
def job_facets(q: str | None = None, relevant_only: bool = True,
               fit_min: int | None = None, tier_min: int | None = None,
               status: list[Literal[(*store.STATUSES, "none")]] | None = Query(None),
               hide_passed: bool = True,
               category: list[str] | None = Query(None), location: str | None = None,
               source: list[str] | None = Query(None),
               posted_within_days: float | None = None,
               active_only: bool = True, run_id: int | None = None,
               sponsorship: Literal["yes", "no"] | None = None,
               salary_min: int | None = Query(None, ge=0)):
    return store.facets(
        q=q, relevant_only=relevant_only, fit_min=fit_min, tier_min=tier_min,
        status=status, hide_passed=hide_passed, category=category, location=location,
        source=source, posted_within_days=posted_within_days, active_only=active_only,
        run_id=run_id, sponsorship=sponsorship, salary_min=salary_min)


@router.get("/api/insights/skills")
def skills(limit: int = Query(100, ge=1, le=1000)):
    return insights.skills_gap(limit)


def _check_links():
    try:
        found = insights.check_links()
    except Exception as e:  # noqa: BLE001 - reported on the event stream
        events.emit("warn", text=f"checking apply links failed: {type(e).__name__}: {e}")
        events.emit("links_done", checked=None, closed=None)
    else:
        events.emit("links_done", checked=found["checked"], closed=found["closed"])
    finally:
        store.close_thread()
        insights.LINK_LOCK.release()


@router.post("/api/insights/links", status_code=202)
def check_links():
    if not insights.LINK_LOCK.acquire(blocking=False):
        raise HTTPException(409, "a link check is already running")
    threading.Thread(target=_check_links, daemon=True).start()
    return {"started": True}


@router.get("/api/insights/why")
def why(days: int = Query(30, ge=1, le=365)):
    return insights.why_nothing(settings.get(), days)


@router.get("/api/insights/companies")
def companies(limit: int = Query(8, ge=1, le=50)):
    return insights.suggested_companies(limit)


@router.get("/api/insights/stats")
def application_stats():
    return insights.stats()


@router.get("/api/insights/cost")
def cost(days: int = Query(30, ge=1, le=365)):
    return store.call_counts(days)
