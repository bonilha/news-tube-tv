"""Channel management routes — CRUD for YouTube channels."""
from __future__ import annotations

from fastapi import APIRouter, Request, Depends, Form
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from app.auth import verify_credentials
from app.database import get_db
from app.queue import download as queue_download
from app.queue import service as queue_svc
from app.yt import invidious

router = APIRouter(tags=["channels"])
templates = Jinja2Templates(directory="app/templates")


def _html(request: Request, name: str, ctx: dict | None = None) -> HTMLResponse:
    is_htmx = request.headers.get("HX-Request") == "true"
    context = {"htmx": is_htmx, **(ctx or {})}
    return templates.TemplateResponse(request=request, name=name, context=context)


# ---------------------------------------------------------------------------
# Full page
# ---------------------------------------------------------------------------

@router.get("/channels", response_class=HTMLResponse)
async def channels_page(request: Request, _auth: bool = Depends(verify_credentials)):
    channels = await _list_channels()
    return _html(request, "channels.html", {"channels": channels})


# ---------------------------------------------------------------------------
# HTMX fragments
# ---------------------------------------------------------------------------

@router.get("/channels/table", response_class=HTMLResponse)
async def channels_table(request: Request, _auth: bool = Depends(verify_credentials)):
    channels = await _list_channels()
    return _html(request, "partials/channel_table.html", {"channels": channels})


@router.post("/channels/add", response_class=HTMLResponse)
async def add_channel(
    request: Request,
    channel_url: str = Form(...),
    min_age_hours: float = Form(2.0),
    _auth: bool = Depends(verify_credentials),
):
    try:
        info = await invidious.resolve_channel(channel_url)
        channel_id = info.get("authorId") or info.get("ucid") or ""
        if not channel_id:
            raise invidious.InvidiousError("Canal resolvido sem identificador")
        name = info.get("author") or channel_url
        handle = ""
        author_url = info.get("authorUrl") or ""
        if "/@" in author_url:
            handle = "@" + author_url.split("/@")[-1].strip("/")

        async with get_db() as db:
            cursor = await db.execute(
                "INSERT OR IGNORE INTO channels (name, channel_id, handle, min_age_hours) VALUES (?, ?, ?, ?)",
                (name, channel_id, handle, min_age_hours),
            )
            await db.commit()
            if cursor.rowcount == 0:
                raise invidious.InvidiousError("Canal já cadastrado")
        stats = await queue_svc.maintain_channel(channel_id, min_age_hours, name)
    except invidious.InvidiousError as e:
        return _html(request, "partials/channel_table.html", {
            "channels": await _list_channels(),
            "error": str(e),
        })

    channels = await _list_channels()
    return _html(request, "partials/channel_table.html", {
        "channels": channels,
        "scan_stats": stats,
    })


@router.post("/channels/{channel_db_id}/toggle", response_class=HTMLResponse)
async def toggle_channel(
    request: Request,
    channel_db_id: int,
    _auth: bool = Depends(verify_credentials),
):
    async with get_db() as db:
        await db.execute(
            "UPDATE channels SET active = 1 - active WHERE id = ?",
            (channel_db_id,),
        )
        await db.commit()
    channels = await _list_channels()
    return _html(request, "partials/channel_table.html", {"channels": channels})


@router.post("/channels/{channel_db_id}/update", response_class=HTMLResponse)
async def update_channel(
    request: Request,
    channel_db_id: int,
    min_age_hours: float = Form(2.0),
    _auth: bool = Depends(verify_credentials),
):
    async with get_db() as db:
        await db.execute(
            "UPDATE channels SET min_age_hours = ? WHERE id = ?",
            (min_age_hours, channel_db_id),
        )
        await db.commit()
    channels = await _list_channels()
    return _html(request, "partials/channel_table.html", {"channels": channels})


@router.delete("/channels/{channel_db_id}", response_class=HTMLResponse)
async def delete_channel(
    request: Request,
    channel_db_id: int,
    _auth: bool = Depends(verify_credentials),
):
    async with get_db() as db:
        await db.execute("DELETE FROM queue WHERE channel_id = ?", (channel_db_id,))
        await db.execute("DELETE FROM channels WHERE id = ?", (channel_db_id,))
        await db.commit()
    queue_download.schedule_sync()
    channels = await _list_channels()
    return _html(request, "partials/channel_table.html", {"channels": channels})


# ---------------------------------------------------------------------------
# JSON API
# ---------------------------------------------------------------------------

@router.get("/api/channels", response_class=JSONResponse)
async def api_list_channels(_auth: bool = Depends(verify_credentials)):
    return JSONResponse(await _list_channels())


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _list_channels() -> list[dict]:
    async with get_db() as db:
        cursor = await db.execute(
            "SELECT id, name, channel_id, handle, min_age_hours, active, created_at "
            "FROM channels ORDER BY created_at DESC"
        )
        return [dict(row) for row in await cursor.fetchall()]
