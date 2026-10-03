from __future__ import annotations

import json

from fastapi import APIRouter, Form, Request, Depends
from fastapi.responses import HTMLResponse, JSONResponse
from urllib.parse import quote

from fastapi.templating import Jinja2Templates

from app.auth import verify_credentials
from app.broadcast import format_hm, get_control, save_limit_hours, start_transmission
from app.cycle import cycle_manager
from app.obs.manager import obs_manager, ALL_SCENES, SCENE_DESCRIPTIONS
from app.queue import service as queue_svc

router = APIRouter(tags=["dashboard"])
templates = Jinja2Templates(directory="app/templates")
templates.env.filters["urlencode"] = lambda value: quote(str(value), safe="")


def _html(request: Request, name: str, ctx: dict | None = None) -> HTMLResponse:
    is_htmx = request.headers.get("HX-Request") == "true"
    context = {"htmx": is_htmx, **(ctx or {})}
    return templates.TemplateResponse(request=request, name=name, context=context)


# ---------------------------------------------------------------------------
# Full page
# ---------------------------------------------------------------------------

@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request, _auth: bool = Depends(verify_credentials)):
    return _html(request, "dashboard.html")


# ---------------------------------------------------------------------------
# HTMX fragments
# ---------------------------------------------------------------------------

@router.get("/scenes", response_class=HTMLResponse)
async def scenes_fragment(request: Request, _auth: bool = Depends(verify_credentials)):
    status = await obs_manager.get_status()
    return _html(request, "scenes.html", {"scenes": status["scenes"]})


@router.get("/assets", response_class=HTMLResponse)
async def assets_fragment(request: Request, _auth: bool = Depends(verify_credentials)):
    from app.assets.service import asset_service
    assets = await asset_service.list_files()
    return _html(request, "assets.html", {"assets": assets})


# ---------------------------------------------------------------------------
# JSON API
# ---------------------------------------------------------------------------

@router.get("/api/status")
async def api_status(_auth: bool = Depends(verify_credentials)):
    return JSONResponse(await obs_manager.get_status())


@router.post("/api/obs/reconnect", response_class=JSONResponse)
async def api_obs_reconnect(_auth: bool = Depends(verify_credentials)):
    await obs_manager.disconnect()
    ok = await obs_manager.connect()
    return JSONResponse({"ok": ok, "connected": ok})


@router.post("/api/scenes/{scene_name}/switch", response_class=JSONResponse)
async def api_switch_scene(scene_name: str, _auth: bool = Depends(verify_credentials)):
    await obs_manager.switch_scene(scene_name)
    return JSONResponse({"ok": True, "current_scene": obs_manager.current_scene})


@router.get("/dashboard/live", response_class=HTMLResponse)
async def dashboard_live(request: Request, _auth: bool = Depends(verify_credentials)):
    status = await obs_manager.get_status()
    cycle = cycle_manager.status_dict()
    control = await get_control()
    limit = int(control.get("limit_seconds") or 0)
    return _html(request, "partials/dashboard_live.html", {
        "status": status,
        "cycle": cycle,
        "queue_ready": await queue_svc.broadcast_ready(),
        "control": control,
        "limit_hours": limit / 3600 if limit else 12,
        "elapsed_label": format_hm(cycle.get("on_air_seconds") or 0),
        "limit_label": format_hm(limit),
    })


@router.post("/api/streaming/start", response_class=JSONResponse)
async def api_start_streaming(_auth: bool = Depends(verify_credentials)):
    result = await start_transmission()
    return JSONResponse({
        "ok": result.get("ok"),
        "error": result.get("error") or "",
        "is_streaming": obs_manager.is_streaming,
    })


@router.post("/api/streaming/stop", response_class=JSONResponse)
async def api_stop_streaming(_auth: bool = Depends(verify_credentials)):
    await obs_manager.stop_streaming()
    return JSONResponse({"ok": True, "is_streaming": False})


@router.post("/api/streaming/finish", response_class=JSONResponse)
async def api_finish_streaming(_auth: bool = Depends(verify_credentials)):
    """Let the current video end, play the bumper, then stop RTMP and the cycle."""
    if not cycle_manager.is_enabled:
        return JSONResponse({"ok": False, "error": "O ciclo não está ligado."})
    cycle_manager.request_finish()
    return JSONResponse({"ok": True, "finish_requested": True})


@router.post("/api/broadcast/limit", response_class=JSONResponse)
async def api_broadcast_limit(
    hours: float = Form(...),
    _auth: bool = Depends(verify_credentials),
):
    try:
        await save_limit_hours(hours)
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)})
    return JSONResponse({"ok": True})


@router.get("/api/cycle/status", response_class=JSONResponse)
async def api_cycle_status(_auth: bool = Depends(verify_credentials)):
    payload = cycle_manager.status_dict()
    payload["queue_ready"] = await queue_svc.broadcast_ready()
    return JSONResponse(payload)


@router.post("/api/cycle/enable", response_class=JSONResponse)
async def api_cycle_enable(_auth: bool = Depends(verify_credentials)):
    result = await cycle_manager.enable()
    return JSONResponse(result)


@router.post("/api/cycle/disable", response_class=JSONResponse)
async def api_cycle_disable(_auth: bool = Depends(verify_credentials)):
    cycle_manager.disable()
    return JSONResponse({"ok": True})
