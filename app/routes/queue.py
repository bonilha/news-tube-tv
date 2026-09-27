"""Queue management routes — view, scan, remove, mark status."""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Request, Depends
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from app.auth import verify_credentials
from app.config import settings
from app.cycle import cycle_manager
from app.queue import cookies as yt_cookies
from app.queue import download as queue_download
from app.queue import service as queue_svc

router = APIRouter(tags=["queue"])
templates = Jinja2Templates(directory="app/templates")
_SP = ZoneInfo("America/Sao_Paulo")


def _timestamp_fmt(value) -> str:
    try:
        ts = int(value)
    except (TypeError, ValueError):
        return "—"
    if ts <= 0:
        return "—"
    return datetime.fromtimestamp(ts, _SP).strftime("%d/%m/%Y %H:%M")


templates.env.filters["timestamp_fmt"] = _timestamp_fmt


async def _queue_ctx(extra: dict | None = None) -> dict:
    extra = extra or {}
    ctx = {
        "encoder": queue_download.selected_encoder_label(),
        "download_keep": settings.QUEUE_DOWNLOAD_KEEP,
        "max_duration_minutes": settings.QUEUE_MAX_DURATION_MINUTES,
        **await cycle_manager.marks_for(extra.get("items") or []),
    }
    if extra:
        ctx.update(extra)
    return ctx


def _html(request: Request, name: str, ctx: dict | None = None) -> HTMLResponse:
    is_htmx = request.headers.get("HX-Request") == "true"
    context = {"htmx": is_htmx, **(ctx or {})}
    return templates.TemplateResponse(request=request, name=name, context=context)


# ---------------------------------------------------------------------------
# Full page
# ---------------------------------------------------------------------------

@router.get("/queue", response_class=HTMLResponse)
async def queue_page(request: Request, _auth: bool = Depends(verify_credentials)):
    items = await queue_svc.get_queue_full()
    return _html(request, "queue.html", await _queue_ctx({"items": items}))


# ---------------------------------------------------------------------------
# HTMX fragments
# ---------------------------------------------------------------------------

@router.get("/queue/table", response_class=HTMLResponse)
async def queue_table(request: Request, _auth: bool = Depends(verify_credentials)):
    items = await queue_svc.get_queue_full()
    return _html(request, "partials/queue_table.html", await _queue_ctx({"items": items}))


@router.post("/queue/shuffle", response_class=HTMLResponse)
async def shuffle_queue(request: Request, _auth: bool = Depends(verify_credentials)):
    await queue_svc.shuffle_play_order(reset_files=True)
    items = await queue_svc.get_queue_full()
    return _html(request, "partials/queue_table.html", await _queue_ctx({"items": items}))


@router.post("/queue/scan", response_class=HTMLResponse)
async def scan_channels(request: Request, _auth: bool = Depends(verify_credentials)):
    stats = await queue_svc.maintain_queue()
    items = await queue_svc.get_queue_full()
    return _html(request, "partials/queue_table.html", await _queue_ctx({
        "items": items,
        "scan_stats": stats,
        "expired_count": stats["expired"],
    }))


@router.post("/queue/{item_id}/status/{status}", response_class=HTMLResponse)
async def update_status(
    request: Request, item_id: int, status: str,
    _auth: bool = Depends(verify_credentials),
):
    await queue_svc.mark_status(item_id, status)
    items = await queue_svc.get_queue_full()
    return _html(request, "partials/queue_table.html", await _queue_ctx({"items": items}))


@router.delete("/queue/{item_id}", response_class=HTMLResponse)
async def remove_item(
    request: Request, item_id: int,
    _auth: bool = Depends(verify_credentials),
):
    await queue_svc.remove_from_queue(item_id)
    items = await queue_svc.get_queue_full()
    return _html(request, "partials/queue_table.html", await _queue_ctx({"items": items}))


@router.post("/queue/clear", response_class=HTMLResponse)
async def clear_queue(request: Request, _auth: bool = Depends(verify_credentials)):
    stats = await queue_svc.clear_queue_and_rescan()
    items = await queue_svc.get_queue_full()
    return _html(request, "partials/queue_table.html", await _queue_ctx({
        "items": items,
        "scan_stats": stats,
        "expired_count": stats.get("expired", 0),
    }))


@router.post("/queue/expire", response_class=HTMLResponse)
async def expire_old(request: Request, _auth: bool = Depends(verify_credentials)):
    stats = await queue_svc.maintain_queue()
    items = await queue_svc.get_queue_full()
    return _html(request, "partials/queue_table.html", await _queue_ctx({
        "items": items,
        "scan_stats": stats,
        "expired_count": stats["expired"],
    }))


@router.post("/queue/{item_id}/retry", response_class=HTMLResponse)
async def retry_download(
    request: Request, item_id: int,
    _auth: bool = Depends(verify_credentials),
):
    await queue_download.reset_video_error(item_id)
    items = await queue_svc.get_queue_full()
    return _html(request, "partials/queue_table.html", await _queue_ctx({"items": items}))


# ---------------------------------------------------------------------------
# JSON API
# ---------------------------------------------------------------------------

@router.get("/api/queue/cookies", response_class=JSONResponse)
async def api_cookie_status(_auth: bool = Depends(verify_credentials)):
    return JSONResponse(yt_cookies.status())


@router.get("/api/queue", response_class=JSONResponse)
async def api_queue(status: str = "pending", _auth: bool = Depends(verify_credentials)):
    return JSONResponse(await queue_svc.get_queue(status))


@router.get("/api/queue/next", response_class=JSONResponse)
async def api_queue_next(_auth: bool = Depends(verify_credentials)):
    video = await queue_svc.get_next_video()
    return JSONResponse(video or {})


@router.post("/api/queue/scan", response_class=JSONResponse)
async def api_scan(_auth: bool = Depends(verify_credentials)):
    stats = await queue_svc.maintain_queue()
    return JSONResponse(stats)
