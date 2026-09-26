"""Conversions between stored rows and the dicts the rest of Cairn reads."""
import json


def _json_list(value):
    return None if value is None else json.dumps(list(value), ensure_ascii=False)


def _list(text):
    return json.loads(text) if text else []


_POSTING_COLUMNS = ("id", "source", "company", "title", "url", "url_key", "locations",
                    "category", "terms", "degrees", "sponsorship", "company_url",
                    "active", "visible", "posted_at", "updated_at", "first_seen_at",
                    "last_seen_at", "link_status", "relevant")


def _fold_detail(job):
    """Replace the flat feedback and salary columns of a _DETAIL row with one value each."""
    verdict, reason = job.pop("feedback_verdict"), job.pop("feedback_reason")
    job["feedback"] = {"verdict": verdict, "reason": reason} if verdict else None
    salary = {"min": job.pop("salary_min"), "max": job.pop("salary_max"),
              "period": job.pop("salary_period"), "currency": job.pop("salary_currency")}
    job["salary"] = salary if salary["min"] is not None or salary["max"] is not None else None


def _posting(row):
    """A stored posting in the raw feed shape, plus whatever the joins brought."""
    job = {"id": row["id"], "source": row["source"], "company_name": row["company"],
           "title": row["title"], "url": row["url"], "locations": _list(row["locations"]),
           "category": row["category"], "terms": _list(row["terms"]),
           "degrees": _list(row["degrees"]), "sponsorship": row["sponsorship"],
           "company_url": row["company_url"], "active": bool(row["active"]),
           "is_visible": bool(row["visible"]),
           "date_posted": row["posted_at"], "date_updated": row["updated_at"],
           "first_seen_at": row["first_seen_at"], "last_seen_at": row["last_seen_at"],
           "closed": row["link_status"] == "closed"}
    for name in sorted(set(row.keys()) - set(_POSTING_COLUMNS)):
        job[name] = row[name]
    if "feedback_verdict" in job:
        _fold_detail(job)
    for name in ("below_floor", "seen"):
        if job.get(name) is not None:
            job[name] = bool(job[name])
    return job
