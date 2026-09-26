"""The FastAPI app behind the native window: the route modules, the error
handlers, and the static page that talks to them.

Handlers validate input and call store, pipeline, and settings; nothing else lives
in the route modules. Every error body is {"error": message}.
"""
import contextlib
import mimetypes
from pathlib import Path

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from cairn.ai import onboard
from cairn.core import paths, settings
from cairn.jobs import logos, pipeline
from cairn.server import insights as insights_routes
from cairn.server import jobs as job_routes
from cairn.server import llm as llm_routes
from cairn.server import onboard as onboard_routes
from cairn.server import runs as run_routes
from cairn.server import settings as settings_routes
from cairn.server import status as status_routes
from cairn.server import system
from cairn.server import tracking as tracking_routes
from cairn.server.security import LocalOnly, _error
from cairn.system import backup

STATIC = Path(__file__).parent / "static"
# Windows takes these from the registry, which can call .js text/plain, and nosniff
# then blocks every module script
for _ext, _kind in ((".js", "text/javascript"), (".css", "text/css"), (".html", "text/html"),
                    (".svg", "image/svg+xml"), (".woff2", "font/woff2"), (".txt", "text/plain")):
    mimetypes.add_type(_kind, _ext)

# the favicon takes its light and dark colours from an inline <style>
FAVICON_CSP = "default-src 'none'; style-src 'unsafe-inline'"


class RevalidatedStaticFiles(StaticFiles):
    """Static files the browser must revalidate on every load.

    Without it a browser, and the native window's WebView, keeps running the old
    scripts and stylesheets after the package is upgraded until a hard reload.
    """

    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache"
        if path == "favicon.svg":
            response.headers["Content-Security-Policy"] = FAVICON_CSP
        return response


def _add_error_handlers(app):
    @app.exception_handler(StarletteHTTPException)
    async def http_error(request, exc):
        return _error(exc.status_code, str(exc.detail))

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request, exc):
        if any(e["type"] == "json_invalid" for e in exc.errors()):
            return _error(400, "request body is not valid JSON")
        problems = ["{}: {}".format(".".join(str(p) for p in e["loc"][1:]) or "body",
                                    e["msg"]) for e in exc.errors()]
        return _error(400, "; ".join(problems))

    @app.exception_handler(pipeline.RunInProgress)
    async def run_in_progress(request, exc):
        return _error(409, str(exc))

    @app.exception_handler(onboard.OnboardError)
    async def bad_onboarding(request, exc):
        return _error(400, str(exc))

    @app.exception_handler(onboard.ClaudeFailed)
    async def claude_failed(request, exc):
        return _error(502, str(exc))

    @app.exception_handler(onboard.FilesExist)
    async def files_exist(request, exc):
        return JSONResponse({"error": str(exc), "files": [str(p) for p in exc.paths]},
                            status_code=409)

    @app.exception_handler(settings.SettingsError)
    async def bad_settings(request, exc):
        return _error(400, str(exc))

    @app.exception_handler(Exception)
    async def unexpected(request, exc):
        return _error(500, f"{type(exc).__name__}: {exc}")


@contextlib.asynccontextmanager
async def _serving(app):
    with backup.serving():
        yield


def create_app():
    paths.lock_down()
    app = FastAPI(title="Cairn", lifespan=_serving)
    # an open event stream never ends by itself, so it ends when the server is stopping
    app.state.shutting_down = lambda: False
    app.state.maintenance = system.maintenance
    app.state.wanted = logos.Wanted(status_routes.fetch_wanted)
    app.add_middleware(LocalOnly, maintenance=system.maintenance)
    _add_error_handlers(app)
    # insights' /api/jobs/facets must register before /api/jobs/{posting_id}
    for module in (insights_routes, job_routes, settings_routes, run_routes, status_routes,
                   onboard_routes, llm_routes, system, tracking_routes):
        app.include_router(module.router)
    # "/" matches every path, so the static mount goes after every API route
    app.mount("/", RevalidatedStaticFiles(directory=STATIC, html=True), name="static")
    return app
