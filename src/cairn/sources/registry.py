"""Source specs, and the adapter that fetches each kind."""
from dataclasses import dataclass
from urllib.parse import urlparse

from cairn.core import events
from cairn.sources.boards import (
    ashby,
    bamboohr,
    greenhouse,
    lever,
    smartrecruiters,
    workable,
)
from cairn.sources.common import Skipped
from cairn.sources.github import github_listings, github_readme
from cairn.sources.hn import hn_hiring
from cairn.sources.job_sites import remoteok, usajobs, yc_waas
from cairn.sources.pages import page
from cairn.sources.workday_jobs import workday

BOARD_KINDS = ("greenhouse", "lever", "ashby", "smartrecruiters", "workable", "bamboohr",
               "workday")
KINDS = ("github", *BOARD_KINDS, "github_readme", "hn_hiring", "yc_waas", "remoteok",
         "usajobs", "page")


@dataclass(frozen=True)
class Source:
    kind: str
    # the listings.json URL for github, the raw Markdown file URL for github_readme,
    # "<tenant>.<wdN>/<site>" for workday, HN_LATEST or a thread URL for hn_hiring,
    # the search keyword for usajobs, a URL for yc_waas, remoteok and page, and the
    # board slug otherwise
    location: str
    company: str | None = None
    enabled: bool = True

    def __post_init__(self):
        if self.kind in BOARD_KINDS and not self.company:
            slug = self.location.split(".")[0] if self.kind == "workday" else self.location
            object.__setattr__(self, "company", slug.title())

    @property
    def name(self):
        """The label stored in the postings.source column and shown in logs."""
        if self.kind == "github":
            owner, repo = urlparse(self.location).path.strip("/").split("/")[:2]
            return f"{owner}/{repo}"
        if self.kind == "github_readme":
            # one repo can publish several lists, and each needs its own name
            owner, repo, _, *path = urlparse(self.location).path.strip("/").split("/")
            file = "/".join(path)
            return f"{owner}/{repo}" if file == "README.md" else f"{owner}/{repo}/{file}"
        return f"{self.kind}:{self.location}"


_FEEDS = {"github": github_listings, "github_readme": github_readme,
          "hn_hiring": hn_hiring, "yc_waas": yc_waas, "remoteok": remoteok,
          "usajobs": usajobs}

# each takes the location and the company name
_BOARDS = {"greenhouse": greenhouse, "lever": lever, "ashby": ashby,
           "smartrecruiters": smartrecruiters, "workable": workable, "bamboohr": bamboohr,
           "workday": workday, "page": page}


def fetch(spec):
    if spec.kind in _FEEDS:
        return _FEEDS[spec.kind](spec.location)
    return _BOARDS[spec.kind](spec.location, spec.company)


def fetch_all(specs):
    """(source name, rows) for every source that answered.

    A source that fails or is skipped is left out, so its stored postings are not
    mistaken for delisted ones.
    """
    batches = []
    for spec in specs:
        try:
            rows = fetch(spec)
        except Skipped as e:
            events.emit("warn", text=f"[fetch] skipped {spec.name}: {e}")
            continue
        except Exception as e:  # noqa: BLE001 - keep going if one source is down
            events.emit("warn", text=f"[fetch] could not load {spec.name}: {e}")
            continue
        batches.append((spec.name, rows))
    return batches
