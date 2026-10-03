"""Stream destination: an OBS preset, or a custom RTMP URL."""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.auth import verify_credentials
from app.broadcast import get_control, save_schedule
from app.obs.services import locate_services_file
from app.obs.stream_config import get_stream_config, save_stream_config, services_for
from app.queue import cookies as yt_cookies

router = APIRouter(tags=["configs"])
templates = Jinja2Templates(directory="app/templates")


def _html(request: Request, ctx: dict) -> HTMLResponse:
    is_htmx = request.headers.get("HX-Request") == "true"
    return templates.TemplateResponse(
        request=request,
        name="configs.html",
        context={"htmx": is_htmx, **ctx},
    )


async def _page_context(config: dict) -> dict:
    services, found = services_for(config)
    saved = (config.get("services_path") or "").strip()
    missing = bool(saved) and locate_services_file(saved) is None
    return {
        "config": config,
        "services": services,
        "services_found": found,
        "services_missing": missing,
        "services_json": json.dumps(services),
        "cookie_state": yt_cookies.status(),
        "cookie_present": yt_cookies.cookie_file().is_file(),
        "control": await get_control(),
    }


@router.get("/configs", response_class=HTMLResponse)
async def configs_page(request: Request, _auth: bool = Depends(verify_credentials)):
    config = await get_stream_config()
    return _html(request, await _page_context(config))


@router.post("/configs", response_class=HTMLResponse)
async def configs_save(
    request: Request,
    mode: str = Form("custom"),
    service: str = Form(""),
    region: str = Form(""),
    stream_key: str = Form(""),
    server_url: str = Form(""),
    services_path: str = Form(""),
    _auth: bool = Depends(verify_credentials),
):
    await save_stream_config(
        mode=mode,
        service=service,
        region=region,
        stream_key=stream_key,
        server_url=server_url,
        services_path=services_path,
    )
    config = await get_stream_config()
    ctx = await _page_context(config)
    ctx["saved"] = True
    return _html(request, ctx)


@router.post("/configs/cookies", response_class=HTMLResponse)
async def configs_cookies_upload(
    request: Request,
    cookies_file: UploadFile = File(...),
    _auth: bool = Depends(verify_credentials),
):
    raw = await cookies_file.read()
    config = await get_stream_config()
    ctx = await _page_context(config)
    try:
        text = raw.decode("utf-8")
        yt_cookies.store_netscape(text)
    except (UnicodeDecodeError, ValueError) as exc:
        ctx["cookie_error"] = str(exc) if isinstance(exc, ValueError) else (
            "O arquivo tem de ser texto Netscape."
        )
        return _html(request, ctx)
    yt_cookies.check_cookies()
    ctx = await _page_context(config)
    state = yt_cookies.status()
    if state["ok"]:
        ctx["cookie_saved"] = True
    else:
        ctx["cookie_error"] = state["message"]
    return _html(request, ctx)


@router.post("/configs/cookies/edge", response_class=HTMLResponse)
async def configs_cookies_edge(
    request: Request,
    _auth: bool = Depends(verify_credentials),
):
    config = await get_stream_config()
    ctx = await _page_context(config)
    try:
        yt_cookies.export_edge_cookies()
    except yt_cookies.EdgeCookiesError as exc:
        ctx["cookie_error"] = str(exc)
        return _html(request, ctx)
    yt_cookies.check_cookies(try_edge=False)
    ctx = await _page_context(config)
    state = yt_cookies.status()
    if state["ok"]:
        ctx["cookie_saved"] = True
    else:
        ctx["cookie_error"] = state["message"]
    return _html(request, ctx)


@router.post("/configs/schedule", response_class=HTMLResponse)
async def configs_schedule(
    request: Request,
    schedule_mode: str = Form("off"),
    schedule_time: str = Form(""),
    schedule_date: str = Form(""),
    _auth: bool = Depends(verify_credentials),
):
    config = await get_stream_config()
    try:
        await save_schedule(schedule_mode, schedule_time, schedule_date)
    except ValueError as exc:
        ctx = await _page_context(config)
        ctx["schedule_error"] = str(exc)
        ctx["control"] = {
            **ctx["control"],
            "schedule_mode": schedule_mode or "off",
            "schedule_time": schedule_time,
            "schedule_date": schedule_date,
        }
        return _html(request, ctx)
    ctx = await _page_context(config)
    ctx["schedule_saved"] = True
    return _html(request, ctx)
