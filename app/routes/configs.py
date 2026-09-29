"""Stream destination: an OBS preset, or a custom RTMP URL."""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.auth import verify_credentials
from app.obs.services import locate_services_file
from app.obs.stream_config import get_stream_config, save_stream_config, services_for

router = APIRouter(tags=["configs"])
templates = Jinja2Templates(directory="app/templates")


def _html(request: Request, ctx: dict) -> HTMLResponse:
    is_htmx = request.headers.get("HX-Request") == "true"
    return templates.TemplateResponse(
        request=request,
        name="configs.html",
        context={"htmx": is_htmx, **ctx},
    )


def _page_context(config: dict) -> dict:
    services, found = services_for(config)
    saved = (config.get("services_path") or "").strip()
    missing = bool(saved) and locate_services_file(saved) is None
    return {
        "config": config,
        "services": services,
        "services_found": found,
        "services_missing": missing,
        "services_json": json.dumps(services),
    }


@router.get("/configs", response_class=HTMLResponse)
async def configs_page(request: Request, _auth: bool = Depends(verify_credentials)):
    config = await get_stream_config()
    return _html(request, _page_context(config))


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
    ctx = _page_context(config)
    ctx["saved"] = True
    return _html(request, ctx)
