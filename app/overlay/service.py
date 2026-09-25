"""OBS lower third: QR browser source plus three text sources on SCENE_PROGRAM."""
from __future__ import annotations

import logging
import re
import winreg
from functools import lru_cache
from pathlib import Path

from app.config import settings
from app.database import get_db
from app.obs.manager import SCENE_PROGRAM, obs_manager
from app.overlay.copy import channel_label, published_line, youtube_url
from app.overlay.title import fit_line

log = logging.getLogger(__name__)

QR_SOURCE = "NewsTube QR"
DATE_SOURCE = "NewsTube Data"
TITLE_SOURCE = "NewsTube Título"
CHANNEL_SOURCE = "NewsTube Canal"
OVERLAY_SOURCES = (QR_SOURCE, DATE_SOURCE, TITLE_SOURCE, CHANNEL_SOURCE)
_TEXT_SOURCES = (DATE_SOURCE, TITLE_SOURCE, CHANNEL_SOURCE)

_TEXT_KINDS = ("text_ft2_source_v2", "text_gdiplus_v2")

# Defaults near C:\temp\exemplo.png on a 1920×1080 canvas. Applied only when the item is created.
_QR_SIZE = 280
_TEXT_X = 360


def qr_page_url() -> str:
    return settings.OVERLAY_BASE_URL.rstrip("/") + "/overlay/qr"


def is_text_kind(kind: str) -> bool:
    return kind.startswith("text_gdiplus") or kind.startswith("text_ft2")


@lru_cache(maxsize=1)
def installed_font_faces() -> tuple[str, ...]:
    """Windows font families, without the Bold/Italic file suffix."""
    styles = ("Bold Italic", "Bold", "Italic", "Regular", "Black", "Light", "Medium")
    found: set[str] = set()
    paths = (
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Fonts"),
        (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Fonts"),
    )
    for hive, path in paths:
        try:
            key = winreg.OpenKey(hive, path)
        except OSError:
            continue
        index = 0
        while True:
            try:
                name, _, _ = winreg.EnumValue(key, index)
            except OSError:
                break
            index += 1
            face = name.split(" (")[0].strip()
            for style in styles:
                if face.endswith(" " + style):
                    face = face[: -(len(style) + 1)]
            if face:
                found.add(face)
    if not found:
        found.update({"Arial", "Segoe UI", "Calibri", "Tahoma", "Verdana"})
    return tuple(sorted(found))


def color_to_obs(hex_color: str) -> int:
    """#RRGGBB → OBS text color integer (0xRRGGBB)."""
    text = (hex_color or "").strip().lstrip("#")
    if len(text) != 6:
        return 0xFFFFFF
    try:
        return int(text, 16) & 0xFFFFFF
    except ValueError:
        return 0xFFFFFF


def color_from_obs(value: int) -> str:
    return f"#{int(value) & 0xFFFFFF:06X}"


_PLAYING = {
    "OBS_MEDIA_STATE_PLAYING",
    "OBS_MEDIA_STATE_OPENING",
    "OBS_MEDIA_STATE_BUFFERING",
    "OBS_MEDIA_STATE_PAUSED",
}


async def video_id_on_player() -> str:
    """Queue video whose file is actually open on NewsTube Fila."""
    try:
        settings_resp = await obs_manager.get_input_settings("NewsTube Fila")
        media = await obs_manager.get_media_input_status("NewsTube Fila")
    except Exception:
        log.debug("Could not read the queue player")
        return ""
    state = str(media.get("mediaState") or "")
    local = str((settings_resp.get("inputSettings") or {}).get("local_file") or "")
    if state not in _PLAYING or not local:
        return ""
    try:
        playing = str(Path(local).resolve())
    except OSError:
        playing = local
    async with get_db() as db:
        cursor = await db.execute(
            "SELECT video_id, local_path FROM queue WHERE local_path != ''"
        )
        rows = await cursor.fetchall()
    for row in rows:
        path = row["local_path"] or ""
        try:
            row_path = str(Path(path).resolve())
        except OSError:
            row_path = path
        if row_path == playing:
            return row["video_id"] or ""
    stem = Path(playing).stem
    if re.fullmatch(r"[A-Za-z0-9_-]{11}", stem):
        return stem
    return ""


async def video_payload(video_id: str) -> dict | None:
    if not video_id:
        return None
    async with get_db() as db:
        cursor = await db.execute(
            """SELECT q.video_id, q.title, q.author, q.published_unix, c.handle
               FROM queue q
               LEFT JOIN channels c ON c.id = q.channel_id
               WHERE q.video_id = ?""",
            (video_id,),
        )
        row = await cursor.fetchone()
    if row is None:
        return {
            "originalVideoUrl": youtube_url(video_id),
            "videoTitle": "",
            "channelName": "",
            "publishedLine": "",
        }
    data = dict(row)
    return {
        "originalVideoUrl": youtube_url(data["video_id"]),
        "videoTitle": data.get("title") or "",
        "channelName": channel_label(data.get("handle") or "", data.get("author") or ""),
        "publishedLine": published_line(int(data.get("published_unix") or 0)),
    }


async def _item_id(source_name: str) -> tuple[str, int] | None:
    items = await obs_manager.get_scene_items(SCENE_PROGRAM)
    for item in items:
        if item.get("sourceName") == source_name:
            scene = item.get("_transformScene") or SCENE_PROGRAM
            return scene, int(item.get("sceneItemId") or 0)
    return None


async def _input_names() -> set[str]:
    inputs = await obs_manager.get_input_list()
    return {item.get("inputName") or "" for item in inputs}


async def _place(scene_item_id: int, x: float, y: float, width: float, height: float) -> None:
    await obs_manager.set_scene_item_transform(
        SCENE_PROGRAM, scene_item_id,
        position_x=x, position_y=y, width=width, height=height,
    )


def _text_settings(text: str, *, size: int, color: int, bold: bool, box_w: int, box_h: int) -> dict:
    return {
        "text": text,
        "read_from_file": False,
        "font": {
            "face": "Arial",
            "style": "Bold" if bold else "Regular",
            "size": size,
            "flags": 1 if bold else 0,
        },
        "color": color,
        "opacity": 100,
        "align": "left",
        "valign": "top",
        "extents": True,
        "extents_wrap": True,
        "extents_cx": box_w,
        "extents_cy": box_h,
    }


async def _create_text(name: str, settings_dict: dict) -> dict | None:
    for kind in _TEXT_KINDS:
        created = await obs_manager.create_input(name, kind, settings_dict, scene_name=SCENE_PROGRAM)
        if created:
            return created
    return None


async def ensure_overlay() -> None:
    """Create the four sources once. An existing item keeps the operator's position and type."""
    if not getattr(obs_manager, "_ws", None):
        if not await obs_manager.connect():
            return
    try:
        canvas_w, canvas_h = await obs_manager.get_canvas_size()
    except Exception:
        log.exception("Canvas size unavailable")
        return
    names = await _input_names()
    bottom = canvas_h - 48

    if QR_SOURCE not in names:
        created = await obs_manager.create_input(
            QR_SOURCE,
            "browser_source",
            {
                "url": qr_page_url(),
                "width": _QR_SIZE,
                "height": _QR_SIZE,
                "fps": 15,
                "shutdown": True,
                "reroute_audio": False,
                "css": "body { background-color: rgba(0, 0, 0, 0); margin: 0; overflow: hidden; }",
            },
            scene_name=SCENE_PROGRAM,
        )
        item_id = int((created or {}).get("sceneItemId") or 0)
        if item_id:
            await _place(item_id, 48, bottom - _QR_SIZE, _QR_SIZE, _QR_SIZE)
            await obs_manager.set_scene_item_enabled(SCENE_PROGRAM, item_id, False)
    else:
        current = await obs_manager.get_input_settings(QR_SOURCE)
        current_url = str((current.get("inputSettings") or {}).get("url") or "")
        if current_url != qr_page_url():
            await obs_manager.set_input_settings(QR_SOURCE, {"url": qr_page_url()})
        if await _item_id(QR_SOURCE) is None:
            added = await obs_manager.create_scene_item(SCENE_PROGRAM, QR_SOURCE)
            item_id = int((added or {}).get("sceneItemId") or 0)
            if item_id:
                await _place(item_id, 48, bottom - _QR_SIZE, _QR_SIZE, _QR_SIZE)
                await obs_manager.set_scene_item_enabled(SCENE_PROGRAM, item_id, False)

    box_w = max(400, canvas_w - _TEXT_X - 48)
    specs = (
        (DATE_SOURCE, "13/09/2026 • 16:30 - Publicado há 15 horas", 32, 0xD0D5DD, False, 48),
        (TITLE_SOURCE, "", 48, 0xFFFFFF, True, 72),
        (CHANNEL_SOURCE, "", 64, 0xFFFFFF, True, 84),
    )
    # Stack upward from the bottom: channel, title, date.
    cursor_y = bottom
    for name, sample, size, color, bold, box_h in reversed(specs):
        cursor_y -= box_h
        if name in names:
            if await _item_id(name) is None:
                added = await obs_manager.create_scene_item(SCENE_PROGRAM, name)
                item_id = int((added or {}).get("sceneItemId") or 0)
                if item_id:
                    await _place(item_id, _TEXT_X, cursor_y, box_w, box_h)
                    await obs_manager.set_scene_item_enabled(SCENE_PROGRAM, item_id, False)
            cursor_y -= 8
            continue
        created = await _create_text(
            name,
            _text_settings(sample, size=size, color=color, bold=bold, box_w=box_w, box_h=box_h),
        )
        item_id = int((created or {}).get("sceneItemId") or 0)
        if item_id:
            await _place(item_id, _TEXT_X, cursor_y, box_w, box_h)
            await obs_manager.set_scene_item_enabled(SCENE_PROGRAM, item_id, False)
        cursor_y -= 8


async def set_visible(visible: bool) -> None:
    items = await obs_manager.get_scene_items(SCENE_PROGRAM)
    for item in items:
        if item.get("sourceName") not in OVERLAY_SOURCES:
            continue
        scene = item.get("_transformScene") or SCENE_PROGRAM
        await obs_manager.set_scene_item_enabled(scene, int(item.get("sceneItemId") or 0), visible)


async def _title_limit_px() -> tuple[str, int, bool, float]:
    """Font already on the title source, and the pixels from its X to the right edge."""
    data = await obs_manager.get_input_settings(TITLE_SOURCE)
    style = read_text_style(data.get("inputSettings") or {})
    canvas_w, _ = await obs_manager.get_canvas_size()
    x = float(_TEXT_X)
    scale = 1.0
    found = await _item_id(TITLE_SOURCE)
    if found:
        scene, item_id = found
        tf = await obs_manager.get_scene_item_transform(scene, item_id)
        x = float(tf.get("positionX") or 0)
        scale = float(tf.get("scaleX") or 1) or 1.0
    screen_px = max(0.0, canvas_w - x - 24)
    return style["face"], int(style["size"]), bool(style["bold"]), screen_px / scale


async def show_video(video_id: str) -> None:
    payload = await video_payload(video_id)
    if not payload:
        await set_visible(False)
        return
    face, size, bold, limit = await _title_limit_px()
    title = fit_line(payload["videoTitle"], face, size, bold, limit)
    await obs_manager.set_input_settings(DATE_SOURCE, {"text": payload["publishedLine"]})
    await obs_manager.set_input_settings(TITLE_SOURCE, {
        "text": title,
        "extents_wrap": False,
        "custom_width": False,
        "word_wrap": 0,
    })
    await obs_manager.set_input_settings(CHANNEL_SOURCE, {"text": payload["channelName"]})
    await set_visible(True)


async def hide() -> None:
    try:
        await set_visible(False)
    except Exception:
        log.exception("Could not hide lower third")


def read_text_style(input_settings: dict) -> dict:
    font = input_settings.get("font") or {}
    flags = int(font.get("flags") or 0)
    style = str(font.get("style") or "")
    return {
        "face": font.get("face") or "Arial",
        "size": int(font.get("size") or 48),
        "color": color_from_obs(int(input_settings.get("color") or 0xFFFFFF)),
        "align": input_settings.get("align") or "left",
        "bold": style.lower().startswith("bold") or bool(flags & 1),
    }


def text_style_settings(face: str, size: int, color: str, align: str, bold: bool) -> dict:
    safe_align = align if align in {"left", "center", "right"} else "left"
    faces = set(installed_font_faces())
    safe_face = face if face in faces else "Arial"
    return {
        "font": {
            "face": safe_face,
            "style": "Bold" if bold else "Regular",
            "size": max(8, min(int(size), 400)),
            "flags": 1 if bold else 0,
        },
        "color": color_to_obs(color),
        "align": safe_align,
    }
