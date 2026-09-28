"""NewsTube TV — FastAPI entry point."""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.config import settings
from app.database import init_db
from app.cycle import cycle_manager
from app.obs.manager import obs_manager
from app.auth import SESSION_COOKIE, basic_ok, session_token
from app.queue import cookies as yt_cookies
from app.queue import download as queue_download
from app.queue import service as queue_svc

log = logging.getLogger(__name__)


async def _queue_buffer_loop() -> None:
    """Keep five MP4s ready. Does not scan channels or delete the on-air file."""
    while True:
        minutes = settings.QUEUE_BUFFER_MINUTES
        if minutes <= 0:
            return
        await asyncio.sleep(minutes * 60)
        try:
            queue_download.schedule_sync()
            log.info("Queue buffer check")
        except Exception:
            log.exception("Queue buffer check failed")


async def _queue_refresh_loop() -> None:
    """Shuffle and download in that order on startup, then scan on the interval."""
    try:
        stats = await queue_svc.startup_broadcast_queue()
        log.info("Startup queue: %s", stats)
    except Exception:
        log.exception("Startup queue failed")
    while True:
        minutes = settings.QUEUE_REFRESH_MINUTES
        if minutes <= 0:
            return
        await asyncio.sleep(minutes * 60)
        try:
            stats = await queue_svc.maintain_queue()
            log.info("Queue maintenance: %s", stats)
        except Exception:
            log.exception("Queue maintenance failed")

# Ensure runtime directories exist
settings.ASSETS_DIR.mkdir(parents=True, exist_ok=True)
settings.VIDEOS_DIR.mkdir(parents=True, exist_ok=True)
Path("static").mkdir(parents=True, exist_ok=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    await asyncio.to_thread(queue_download.probe_encoders)
    await asyncio.to_thread(yt_cookies.check_cookies)
    await obs_manager.connect()
    from app.overlay.service import ensure_overlay
    try:
        await ensure_overlay()
    except Exception:
        log.exception("Lower third was not created")
    refresh = asyncio.create_task(_queue_refresh_loop())
    buffer = asyncio.create_task(_queue_buffer_loop())
    yield
    buffer.cancel()
    refresh.cancel()
    try:
        await buffer
    except asyncio.CancelledError:
        pass
    try:
        await refresh
    except asyncio.CancelledError:
        pass
    cycle_manager.disable()
    queue_download.shutdown()
    await obs_manager.disconnect()


app = FastAPI(title="NewsTube TV", lifespan=lifespan)


@app.middleware("http")
async def remember_login(request: Request, call_next):
    """Keep the console login on a cookie so HTMX polls are not anonymous."""
    response = await call_next(request)
    if response.status_code < 400 and basic_ok(_basic_credentials(request)):
        response.set_cookie(
            SESSION_COOKIE,
            session_token(),
            httponly=True,
            samesite="lax",
            path="/",
        )
    return response


def _basic_credentials(request: Request):
    from fastapi.security import HTTPBasicCredentials
    header = request.headers.get("authorization") or ""
    scheme, _, encoded = header.partition(" ")
    if scheme.lower() != "basic" or not encoded:
        return None
    import base64
    try:
        decoded = base64.b64decode(encoded).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return None
    user, sep, password = decoded.partition(":")
    if not sep:
        return None
    return HTTPBasicCredentials(username=user, password=password)

@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    return FileResponse(Path("static/favicon.ico"), media_type="image/x-icon")


# Static files
app.mount("/static", StaticFiles(directory="static"), name="static")
app.mount("/assets_files", StaticFiles(directory=settings.ASSETS_DIR), name="assets_files")

# Routers
from app.routes import dashboard, assets, editor, channels, queue, overlay  # noqa: E402
app.include_router(overlay.router)
app.include_router(dashboard.router)
app.include_router(assets.router)
app.include_router(editor.router)
app.include_router(channels.router)
app.include_router(queue.router)

if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
