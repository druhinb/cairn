"""The posting row every list in the API returns."""
from cairn import store
from cairn.jobs import descriptions


def make_row(job):
    """A store.search or store.get_posting result as an /api/jobs row."""
    return {"id": job["id"], "company": job["company_name"], "title": job["title"],
            "url": store.web_url(job["url"]), "locations": job["locations"],
            "category": job["category"],
            "source": job["source"], "posted_at": job["date_posted"],
            "fit": job.get("fit"), "tier": job.get("tier"),
            "fit_reason": job.get("fit_reason"), "tier_reason": job.get("tier_reason"),
            "below_floor": job.get("below_floor"), "status": job.get("status"),
            "note": job.get("note"), "applied_at": job.get("applied_at"),
            "updated_at": job.get("status_updated_at"), "seen": job["seen"],
            "run_id": job.get("run_id"), "logo_domain": job.get("logo_domain"),
            "applied_before": (job.get("applied_before_at") or None) and
                              job["applied_before_at"][:10],
            "timing": descriptions.timing(job.get("keywords")),
            "starts_before_graduation": descriptions.starts_before_graduation(
                job.get("keywords")),
            "feedback": job.get("feedback"), "salary": job.get("salary"),
            "sponsorship": job.get("offers_sponsorship"), "closed": job.get("closed", False),
            "also_on": job.get("also_on", [])}
