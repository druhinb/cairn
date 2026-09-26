"""Routes for settings, the profile file, and watchlist lookups."""
import dataclasses
import os
import tempfile
from typing import Annotated

from fastapi import APIRouter, Body, HTTPException
from pydantic import BaseModel, StringConstraints

from cairn import sources, store
from cairn.core import paths, settings

router = APIRouter()

EDITABLE = {"profile": paths.profile_md}


class FileBody(BaseModel):
    text: str


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
