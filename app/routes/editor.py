"""Scene editor routes — manipulate OBS sources per scene."""
from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter, Request, Depends, Query
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from app.auth import verify_credentials
from app.assets.service import asset_service
from app.config import settings
from app.obs.manager import SCENE_PROGRAM, obs_manager
from app.overlay.service import (
    installed_font_faces,
    is_text_kind,
    read_text_style,
    text_style_settings,
)

router = APIRouter(tags=["editor"])
templates = Jinja2Templates(directory="app/templates")
templates.env.filters["urlencode"] = lambda value: quote(str(value), safe="")

# MIME → OBS inputKind mapping
_MIME_TO_INPUT_KIND: dict[str, str] = {
    "image/jpeg": "image_source",
    "image/png": "image_source",
    "image/gif": "image_source",
    "image/webp": "image_source",
    "image/svg+xml": "image_source",
    "video/mp4": "ffmpeg_source",
    "video/webm": "ffmpeg_source",
}
_DEFAULT_INPUT_KIND = "ffmpeg_source"


def _input_kind_for_mime(mime_type: str) -> str:
    return _MIME_TO_INPUT_KIND.get(mime_type, _DEFAULT_INPUT_KIND)


def _html(request: Request, name: str, ctx: dict | None = None) -> HTMLResponse:
    is_htmx = request.headers.get("HX-Request") == "true"
    context = {"htmx": is_htmx, **(ctx or {})}
    return templates.TemplateResponse(request=request, name=name, context=context)


# ---------------------------------------------------------------------------
# Full page
# ---------------------------------------------------------------------------

@router.get("/editor/scene-tabs", response_class=HTMLResponse)
async def scene_tabs(
    request: Request,
    scene: str = Query(default=""),
    _auth: bool = Depends(verify_credentials),
):
    scenes = await obs_manager.get_scene_list()
    return _html(request, "editor_tabs.html", {"scenes": scenes, "selected_scene": scene})


@router.get("/editor/available-assets", response_class=HTMLResponse)
async def available_assets(
    request: Request,
    scene_name: str = Query(...),
    _auth: bool = Depends(verify_credentials),
):
    # DB assets that don't have an OBS source yet in this scene
    db_assets = await asset_service.list_files()
    # OBS inputs that already exist (e.g. created directly in OBS or from a previous session)
    obs_inputs = await obs_manager.get_input_list()
    current = await obs_manager.get_scene_items(scene_name)
    current_names = {i.get("sourceName") for i in current}
    available_assets = [a for a in db_assets if a["original_name"] not in current_names]
    # OBS inputs not in current scene and not already covered by a DB asset
    db_names = {a["original_name"] for a in db_assets}
    available_inputs = [i for i in obs_inputs
                        if i.get("inputName") not in current_names
                        and i.get("inputName") not in db_names]
    return _html(request, "editor_add_source.html", {
        "scene_name": scene_name,
        "assets": available_assets,
        "obs_inputs": available_inputs,
    })


@router.get("/editor/scene/{scene_name}/items", response_class=HTMLResponse)
async def scene_items(
    request: Request, scene_name: str,
    _auth: bool = Depends(verify_credentials),
):
    if scene_name == SCENE_PROGRAM:
        from app.overlay.service import ensure_overlay
        try:
            await ensure_overlay()
        except Exception:
            pass
    items = await obs_manager.get_scene_items(scene_name)
    kinds = {
        row.get("inputName"): row.get("inputKind") or ""
        for row in await obs_manager.get_input_list()
    }
    for item in items:
        item["inputKind"] = item.get("inputKind") or kinds.get(item.get("sourceName")) or ""
        owner = item.get("_transformScene") or scene_name
        item["sceneItemTransform"] = await obs_manager.get_scene_item_transform(
            owner, int(item.get("sceneItemId") or 0),
        )
    # OBS lists items bottom → top. Show the front-most source first.
    items = list(reversed(items))
    return _html(request, "editor_items.html", {"scene_name": scene_name, "items": items})


@router.get("/editor/scene/{scene_name}", response_class=HTMLResponse)
async def editor_page_for_scene(
    request: Request, scene_name: str,
    _auth: bool = Depends(verify_credentials),
):
    scenes = await obs_manager.get_scene_list()
    canvas_w, canvas_h = await obs_manager.get_canvas_size()
    return _html(request, "editor.html", {
        "selected_scene": scene_name,
        "scenes": scenes,
        "canvas_width": canvas_w,
        "canvas_height": canvas_h,
    })


@router.get("/editor", response_class=HTMLResponse)
async def editor_page(
    request: Request,
    _auth: bool = Depends(verify_credentials),
):
    scenes = await obs_manager.get_scene_list()
    canvas_w, canvas_h = await obs_manager.get_canvas_size()
    return _html(request, "editor.html", {
        "scenes": scenes,
        "selected_scene": "",
        "canvas_width": canvas_w,
        "canvas_height": canvas_h,
    })


# ---------------------------------------------------------------------------
# JSON API — scene item manipulation
# ---------------------------------------------------------------------------

@router.post("/api/editor/scene/{scene_name}/items/add", response_class=JSONResponse)
async def add_item(
    scene_name: str, request: Request,
    _auth: bool = Depends(verify_credentials),
):
    body = await request.json()
    source_name = body.get("sourceName")  # existing OBS input
    asset_id = body.get("assetId")        # DB asset → create OBS input first

    if source_name:
        # OBS input already exists — just attach it to this scene.
        result = await obs_manager.create_scene_item(scene_name, source_name)
        return JSONResponse({"ok": result is not None, "item": result})

    if not asset_id:
        return JSONResponse({"ok": False, "error": "assetId or sourceName required"}, status_code=400)

    from app.database import get_asset
    asset_record = await get_asset(int(asset_id))
    if not asset_record:
        return JSONResponse({"ok": False, "error": "asset not found"}, status_code=404)

    input_name = asset_record["original_name"]
    mime_type = asset_record.get("mime_type", "application/octet-stream")
    input_kind = _input_kind_for_mime(mime_type)

    # OBS resolves media paths on its own machine, not relative to this process.
    file_path = str((settings.ASSETS_DIR / asset_record["filename"]).resolve())

    input_settings: dict = {}
    if input_kind == "image_source":
        input_settings = {"file": file_path}
    elif input_kind == "ffmpeg_source":
        input_settings = {"is_local_file": True, "local_file": file_path}

    # CreateInput with sceneName both creates the source and places it in the scene.
    created = await obs_manager.create_input(
        input_name, input_kind, input_settings, scene_name=scene_name,
    )
    if created is not None:
        return JSONResponse({"ok": True, "item": created})

    # Source name already exists in OBS — just attach it to this scene.
    result = await obs_manager.create_scene_item(scene_name, input_name)
    return JSONResponse({"ok": result is not None, "item": result})


@router.delete("/api/editor/scene/{scene_name}/items/{item_id}", response_class=JSONResponse)
async def remove_item(
    scene_name: str, item_id: int,
    _auth: bool = Depends(verify_credentials),
):
    ok = await obs_manager.remove_scene_item(scene_name, item_id)
    return JSONResponse({"ok": ok})


@router.put("/api/editor/scene/{scene_name}/items/{item_id}/index", response_class=JSONResponse)
async def reorder_item(
    scene_name: str, item_id: int, request: Request,
    _auth: bool = Depends(verify_credentials),
):
    body = await request.json()
    index = body.get("index", 0)
    ok = await obs_manager.set_scene_item_index(scene_name, item_id, index)
    return JSONResponse({"ok": ok})


@router.put("/api/editor/scene/{scene_name}/items/{item_id}/transform", response_class=JSONResponse)
async def update_transform(
    scene_name: str, item_id: int, request: Request,
    _auth: bool = Depends(verify_credentials),
):
    body = await request.json()
    # Width/height are canvas pixels. OBS stores scale; the manager converts.
    ok = await obs_manager.set_scene_item_transform(
        scene_name, item_id,
        position_x=body.get("positionX"),
        position_y=body.get("positionY"),
        width=body.get("width"),
        height=body.get("height"),
        rotation=body.get("rotation"),
    )
    transform = await obs_manager.get_scene_item_transform(scene_name, item_id)
    return JSONResponse({"ok": ok, "transform": transform})


@router.get("/api/editor/source/{source_name}/text")
async def text_style(source_name: str, _auth: bool = Depends(verify_credentials)):
    data = await obs_manager.get_input_settings(source_name)
    kind = data.get("inputKind") or ""
    if not is_text_kind(kind):
        return JSONResponse({"text": False})
    style = read_text_style(data.get("inputSettings") or {})
    style["text"] = True
    style["faces"] = list(installed_font_faces())
    return JSONResponse(style)


@router.put("/api/editor/source/{source_name}/text")
async def update_text_style(
    source_name: str, request: Request,
    _auth: bool = Depends(verify_credentials),
):
    body = await request.json()
    data = await obs_manager.get_input_settings(source_name)
    if not is_text_kind(data.get("inputKind") or ""):
        return JSONResponse({"ok": False, "error": "Essa fonte não é texto."}, status_code=400)
    ok = await obs_manager.set_input_settings(
        source_name,
        text_style_settings(
            str(body.get("face") or ""),
            int(body.get("size") or 48),
            str(body.get("color") or "#FFFFFF"),
            str(body.get("align") or "left"),
            bool(body.get("bold")),
        ),
    )
    fresh = await obs_manager.get_input_settings(source_name)
    style = read_text_style((fresh or {}).get("inputSettings") or {})
    style["ok"] = ok
    return JSONResponse(style)


@router.put("/api/editor/scene/{scene_name}/items/{item_id}/enabled", response_class=JSONResponse)
async def toggle_item(
    scene_name: str, item_id: int, request: Request,
    _auth: bool = Depends(verify_credentials),
):
    body = await request.json()
    ok = await obs_manager.set_scene_item_enabled(scene_name, item_id, body.get("enabled", True))
    return JSONResponse({"ok": ok})


@router.get("/api/editor/screenshot/{source_name}", response_class=JSONResponse)
async def screenshot(
    source_name: str,
    quality: int = Query(80, ge=1, le=100),
    _auth: bool = Depends(verify_credentials),
):
    data = await obs_manager.get_source_screenshot(source_name, fmt="jpg", quality=quality)
    if data is None:
        return JSONResponse({"ok": False}, status_code=404)
    return JSONResponse({"ok": True, "image": data})
