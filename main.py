"""NewsTube TV — FastAPI entry point."""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.config import settings
from app.database import init_db
from app.cycle import cycle_manager
from app.obs.manager import obs_manager
from app.queue import cookies as yt_cookies
from app.queue import download as queue_download
from app.queue import service as queue_svc

log = logging.getLogger(__name__)


async def _queue_refresh_loop() -> None:
    """Scan and expire on startup, then every QUEUE_REFRESH_MINUTES."""
    while True:
        try:
            stats = await queue_svc.maintain_queue()
            log.info("Queue maintenance: %s", stats)
        except Exception:
            log.exception("Queue maintenance failed")
        minutes = settings.QUEUE_REFRESH_MINUTES
        if minutes <= 0:
            return
        await asyncio.sleep(minutes * 60)

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
    yield
    refresh.cancel()
    try:
        await refresh
    except asyncio.CancelledError:
        pass
    cycle_manager.disable()
    queue_download.shutdown()
    await obs_manager.disconnect()


app = FastAPI(title="NewsTube TV", lifespan=lifespan)

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
