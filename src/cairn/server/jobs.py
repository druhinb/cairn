"""Routes for postings: search, detail, applications, summaries, and score reasons."""
from typing import Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict, model_validator

from cairn import store
from cairn.ai import llm
from cairn.core import events, paths, settings
from cairn.jobs import descriptions, pipeline, rank
from cairn.server import llm as llm_routes
from cairn.server.background import launch
from cairn.server.rows import make_row

router = APIRouter()

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


@router.get("/api/jobs")
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


@router.get("/api/jobs/{posting_id}")
def get_job(posting_id: str):
    return _detail(_posting_or_404(posting_id))


@router.put("/api/jobs/{posting_id}/application")
def put_application(posting_id: str, body: ApplicationBody):
    _posting_or_404(posting_id)
    if body.status is not None:
        store.set_status(posting_id, body.status, body.note)
    elif store.set_note(posting_id, body.note) is None:
        raise HTTPException(409, "the posting has no status to attach a note to")
    job = store.get_posting(posting_id)
    events.emit("application_changed", id=posting_id, status=job["status"])
    return _detail(job)


@router.delete("/api/jobs/{posting_id}/application")
def clear_application(posting_id: str):
    _posting_or_404(posting_id)
    store.clear_application(posting_id)
    events.emit("application_changed", id=posting_id, status=None)
    events.emit("tracking_changed", id=posting_id)
    return _detail(store.get_posting(posting_id))


@router.get("/api/applications")
def list_applications(status: list[Status] | None = Query(None)):
    return {"rows": [{**row, "url": store.web_url(row["url"])}
                     for row in store.applications(status)],
            "counts": store.application_counts()}


@router.post("/api/jobs/{posting_id}/summary", status_code=202)
def summarise_job(posting_id: str, force: bool = False):
    _posting_or_404(posting_id)

    def work(started):
        started()
        pipeline.summarise_posting(posting_id, force=force)

    def failed(e):
        events.emit("warn",
                    text=f"summarising {posting_id} failed: {type(e).__name__}: {e}")

    launch(work, on_failure=failed)
    return {"started": True}


@router.post("/api/jobs/{posting_id}/reasons")
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
