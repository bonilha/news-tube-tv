"""NewsTube TV — FastAPI entry point."""
from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.auth import SessionCookie
from app.config import settings
from app.database import init_db
from app.cycle import cycle_manager
from app.obs.manager import obs_manager
from app.queue import cookies as yt_cookies
from app.queue import download as queue_download
from app.queue import service as queue_svc

log = logging.getLogger(__name__)
_HEARTBEAT_SEC = 15 * 60
_process_started = time.monotonic()


def configure_logging() -> None:
    """Warnings and tracebacks carry the same local clock as the operator lines."""
    logging.basicConfig(
        level=logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        force=True,
    )


configure_logging()


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
    finally:
        _startup_done.set()
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

_startup_done = asyncio.Event()


def format_heartbeat(
    *,
    uptime_sec: int,
    cycle_on: bool,
    streaming: bool,
    download_busy: bool,
    oldest_hours: float | None,
    pending_tasks: int,
) -> str:
    """One operator line. The pending-task count is what grew until the process stalled."""
    hours, minutes = divmod(max(0, uptime_sec) // 60, 60)
    age = "nenhum" if oldest_hours is None else f"{oldest_hours:.1f}h"
    return (
        f"Vivo ha {hours}h{minutes:02d}m | "
        f"ciclo {'ligado' if cycle_on else 'parado'} | "
        f"RTMP {'ligado' if streaming else 'parado'} | "
        f"download {'preso' if download_busy else 'livre'} | "
        f"pendente mais antigo {age} | "
        f"tarefas {pending_tasks}"
    )


async def _heartbeat() -> None:
    try:
        oldest = await queue_svc.oldest_pending_age_hours()
    except Exception:
        log.exception("Heartbeat could not read the queue")
        oldest = None
    pending = sum(1 for task in asyncio.all_tasks() if not task.done())
    queue_download.say(format_heartbeat(
        uptime_sec=int(time.monotonic() - _process_started),
        cycle_on=cycle_manager.is_enabled,
        streaming=obs_manager.is_streaming,
        download_busy=queue_download.download_busy(),
        oldest_hours=oldest,
        pending_tasks=pending,
    ))


async def _broadcast_loop() -> None:
    """Local clock: open downloads before a scheduled start, then go on air."""
    from app.broadcast import tick

    await _startup_done.wait()
    next_beat = 0.0
    while True:
        now = time.monotonic()
        if now >= next_beat:
            next_beat = now + _HEARTBEAT_SEC
            try:
                await _heartbeat()
            except Exception:
                log.exception("Heartbeat failed")
        try:
            await tick()
        except Exception:
            log.exception("Broadcast schedule failed")
        await asyncio.sleep(15)


# Ensure runtime directories exist
settings.ASSETS_DIR.mkdir(parents=True, exist_ok=True)
queue_download.ensure_video_dirs()
Path("static").mkdir(parents=True, exist_ok=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Uvicorn installs its own logging after import. Put the clock back.
    configure_logging()
    await init_db()
    await asyncio.to_thread(queue_download.probe_encoders)
    await asyncio.to_thread(yt_cookies.check_cookies)
    await obs_manager.connect()
    from app.overlay.service import ensure_overlay
    try:
        await ensure_overlay()
    except Exception:
        log.exception("Lower third was not created")
    # Closed until startup_broadcast_queue finishes the scan and the shuffle.
    queue_download.hold_downloads()
    refresh = asyncio.create_task(_queue_refresh_loop())
    buffer = asyncio.create_task(_queue_buffer_loop())
    schedule = asyncio.create_task(_broadcast_loop())
    yield
    schedule.cancel()
    buffer.cancel()
    refresh.cancel()
    try:
        await schedule
    except asyncio.CancelledError:
        pass
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
# HTML routes return their own Response, which drops a cookie set on the
# dependency Response. This wrapper puts the cookie on that start line.
app.add_middleware(SessionCookie)


@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    return FileResponse(Path("static/favicon.ico"), media_type="image/x-icon")


# Static files
app.mount("/static", StaticFiles(directory="static"), name="static")
app.mount("/assets_files", StaticFiles(directory=settings.ASSETS_DIR), name="assets_files")

# Routers
from app.routes import dashboard, assets, editor, channels, queue, overlay, configs  # noqa: E402
app.include_router(overlay.router)
app.include_router(dashboard.router)
app.include_router(assets.router)
app.include_router(editor.router)
app.include_router(channels.router)
app.include_router(queue.router)
app.include_router(configs.router)

if __name__ == "__main__":
    configure_logging()
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=8000,
        reload=True,
        log_level="warning",
        access_log=False,
    )
