"""Routes for settings, the profile file, and watchlist lookups."""
import dataclasses
import os
import tempfile
from typing import Annotated

from fastapi import APIRouter, Body, HTTPException
from pydantic import BaseModel, StringConstraints

from cairn import sources, store
from cairn.sources import watchlists
from cairn.core import paths, settings

router = APIRouter()

EDITABLE = {"profile": paths.profile_md}


class FileBody(BaseModel):
    text: str


class DraftBody(BaseModel):
    watchlist: list[dict]


class ImportBody(DraftBody):
    text: Annotated[str, StringConstraints(max_length=200_000)]


class ResolveBody(BaseModel):
    query: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1,
                                            max_length=200)]


def _write_atomic(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent,
                                     prefix=f".{path.name}.", delete=False) as f:
        f.write(text)
    os.replace(f.name, path)


@router.get("/api/settings")
def get_settings():
    return {**dataclasses.asdict(settings.base()),
            "defaults": dataclasses.asdict(settings.defaults()),
            "path": str(paths.config_file())}


@router.put("/api/settings")
def put_settings(body: dict = Body(...)):
    new = settings.from_dict(body, base=settings.base(), source="request")
    settings.save(new)
    settings.use(new)
    store.sync_relevance()
    return dataclasses.asdict(new)


@router.get("/api/files/{name}")
def get_file(name: str):
    if name not in EDITABLE:
        raise HTTPException(404, f"no such file: {name}")
    path = EDITABLE[name]()
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    return {"name": name, "path": str(path), "text": text}


@router.put("/api/files/{name}")
def put_file(name: str, body: FileBody):
    if name not in EDITABLE:
        raise HTTPException(404, f"no such file: {name}")
    path = EDITABLE[name]()
    _write_atomic(path, body.text)
    return {"name": name, "path": str(path), "text": body.text}


@router.post("/api/watchlist/resolve")
def resolve_watchlist(body: ResolveBody):
    found = sources.resolve_company(body.query)
    if found is None:
        raise HTTPException(404, f"Cairn couldn't find a careers page for {body.query}")
    return {"kind": found.kind, "location": found.location, "company": found.company}


@router.get("/api/watchlist/export")
def export_watchlist():
    return watchlists.export(settings.base().watchlist)


@router.post("/api/watchlist/import")
def import_watchlist(body: ImportBody):
    """body.watchlist with the file's companies merged in; nothing is saved."""
    try:
        name, specs = watchlists.parse(body.text)
    except watchlists.WatchlistFileError as e:
        raise HTTPException(400, str(e)) from None
    return _merged(name, specs, body.watchlist)


@router.get("/api/watchlist/starters")
def starter_watchlists():
    return watchlists.starters()


@router.post("/api/watchlist/starters/{starter_id}")
def add_starter_watchlist(starter_id: str, body: DraftBody):
    """body.watchlist with a packaged list merged in; nothing is saved."""
    try:
        name, _, specs = watchlists.starter(starter_id)
    except KeyError:
        raise HTTPException(404, f"no starter list '{starter_id}'") from None
    return _merged(name, specs, body.watchlist)


def _merged(name, specs, watchlist):
    merged, added, turned_on = watchlists.merge(watchlist, specs)
    return {"name": name, "watchlist": merged, "added": added, "turned_on": turned_on,
            "already": len(specs) - added - turned_on}
