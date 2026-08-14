"""ASGI entry point.

Serves the API under ``/api`` and the single-page frontend from ``/``. Both are
mounted relatively so the whole thing works unchanged under an Ingress path like
``/api/hassio_ingress/xY9.../``, which is not a fixed prefix and cannot be
hard-coded anywhere.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .api import router
from .config import configure_logging, load_options
from .service import Service

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).resolve().parent / "static"

# How often the background loop syncs metrics and republishes sensors. Fitbit
# data arrives via its own cloud polling interval anyway, so anything under about
# ten minutes just burns CPU for no fresher data.
BACKGROUND_INTERVAL_SECONDS = 900


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging(str((load_options().get("log_level")) or "info"))
    service = Service()
    task: asyncio.Task | None = None
    try:
        service.start()
        app.state.service = service
        task = asyncio.create_task(
            service.background_loop(BACKGROUND_INTERVAL_SECONDS),
            name="plate-background",
        )
    except Exception:
        # Starting up degraded beats not starting at all: the UI can then show
        # the user *why* it's broken instead of a blank connection error.
        log.exception("startup failed; running in degraded mode")
        app.state.service = service
    try:
        yield
    finally:
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        await service.stop()


app = FastAPI(
    title="PLATE",
    description="Adaptive menu planning driven by your own health metrics.",
    version="0.1.0",
    lifespan=lifespan,
    # Ingress rewrites the path, so absolute doc URLs would 404. Relative works.
    docs_url="/api/docs",
    redoc_url=None,
    openapi_url="/api/openapi.json",
)

app.include_router(router)


@app.exception_handler(Exception)
async def unhandled(request: Request, exc: Exception) -> JSONResponse:
    """Return something the frontend can display instead of an empty 500."""
    log.exception("unhandled error on %s %s", request.method, request.url.path)
    return JSONResponse(
        status_code=500,
        content={
            "error": type(exc).__name__,
            "detail": str(exc),
            "hint": "Check the add-on log for the full traceback.",
        },
    )


@app.get("/healthz", include_in_schema=False)
async def healthz():
    """Cheap liveness probe that does not touch Home Assistant or the database."""
    service = getattr(app.state, "service", None)
    ready = bool(service and service.library and service.store)
    return JSONResponse({"status": "ok" if ready else "starting"}, status_code=200 if ready else 503)


if STATIC_DIR.is_dir():
    # html=True makes StaticFiles serve index.html for "/", and mounting last
    # means /api and /healthz still win.
    app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
else:  # pragma: no cover - only when the image was built wrong
    @app.get("/")
    async def missing_ui():
        return JSONResponse(
            {"error": "static assets missing", "expected": str(STATIC_DIR)}, status_code=500
        )


@app.get("/manifest.webmanifest", include_in_schema=False)
async def manifest():
    """PWA manifest, served with the right content type.

    Kept as a route rather than a static file only so the media type is certain
    across platforms; StaticFiles guesses from the OS mime registry, which on some
    hosts does not know this extension.
    """
    path = STATIC_DIR / "manifest.webmanifest"
    if not path.is_file():
        return JSONResponse({"error": "not found"}, status_code=404)
    return FileResponse(path, media_type="application/manifest+json")
