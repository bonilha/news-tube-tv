from __future__ import annotations

from fastapi import APIRouter, Request, Depends, HTTPException, UploadFile, File
from fastapi.responses import HTMLResponse, FileResponse, PlainTextResponse
from fastapi.templating import Jinja2Templates

from app.auth import verify_credentials
from app.assets.service import UploadRejected, asset_service
from app.config import settings

router = APIRouter(prefix="/assets", tags=["assets"])
templates = Jinja2Templates(directory="app/templates")


def _html(request: Request, name: str, ctx: dict | None = None) -> HTMLResponse:
    is_htmx = request.headers.get("HX-Request") == "true"
    context = {"htmx": is_htmx, **(ctx or {})}
    return templates.TemplateResponse(request=request, name=name, context=context)


@router.post("/upload", response_class=HTMLResponse)
async def upload_asset(
    request: Request,
    file: UploadFile = File(...),
    _auth: bool = Depends(verify_credentials),
):
    try:
        await asset_service.upload_file(file)
    except UploadRejected as exc:
        return PlainTextResponse(exc.message, status_code=400)
    assets = await asset_service.list_files()
    return _html(request, "partials/asset_table.html", {"assets": assets})


@router.delete("/{asset_id}", response_class=HTMLResponse)
async def delete_asset(
    request: Request,
    asset_id: int,
    _auth: bool = Depends(verify_credentials),
):
    await asset_service.delete_file(asset_id)
    assets = await asset_service.list_files()
    return _html(request, "partials/asset_table.html", {"assets": assets})


@router.get("/{asset_id}/file")
async def serve_asset(asset_id: int, _auth: bool = Depends(verify_credentials)):
    path = await asset_service.get_file_path(asset_id)
    if path is None:
        raise HTTPException(status_code=404, detail="Asset not found")
    return FileResponse(path)
