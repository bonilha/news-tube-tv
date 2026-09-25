"""Lower-third QR page. No console password: OBS Browser Source cannot log in."""
from __future__ import annotations

import io
import re

import segno
from fastapi import APIRouter, Query
from fastapi.responses import HTMLResponse, Response

from app.overlay import service as overlay

router = APIRouter(tags=["overlay"])

_PAGE = """<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
html, body { margin: 0; width: 100%; height: 100%; background: transparent; overflow: hidden; }
.frame {
  box-sizing: border-box;
  width: 100%;
  height: 100%;
  min-width: 0;
  min-height: 0;
  border: 14px solid #FFE14A;
  border-radius: 14px;
  padding: 8px;
  background: #fff;
}
.frame[hidden] { display: none; }
object, img { width: 100%; height: 100%; display: block; object-fit: contain; }
</style>
</head>
<body>
<div class="frame" id="frame" hidden>
  <img id="qr" alt="">
</div>
<script>
async function tick() {
  var frame = document.getElementById('frame');
  var img = document.getElementById('qr');
  try {
    var resp = await fetch('/overlay/now');
    if (!resp.ok) { frame.hidden = true; return; }
    var data = await resp.json();
    if (!data.originalVideoUrl) { frame.hidden = true; img.removeAttribute('src'); return; }
    var next = '/overlay/qr.svg?u=' + encodeURIComponent(data.originalVideoUrl);
    if (img.getAttribute('src') !== next) img.src = next;
    frame.hidden = false;
  } catch (e) {
    frame.hidden = true;
  }
}
tick();
setInterval(tick, 1000);
</script>
</body>
</html>
"""


@router.get("/overlay/qr", response_class=HTMLResponse)
async def qr_page():
    return HTMLResponse(_PAGE)


async def _on_air_payload() -> dict | None:
    from app.cycle import cycle_manager

    video_id = cycle_manager.status_dict().get("current_video_id") or ""
    if not video_id:
        video_id = await overlay.video_id_on_player()
    return await overlay.video_payload(video_id)


@router.get("/overlay/now")
async def overlay_now():
    payload = await _on_air_payload()
    if not payload:
        return {"originalVideoUrl": "", "videoTitle": "", "channelName": "", "publishedLine": ""}
    return payload


@router.get("/overlay/qr.svg")
async def qr_svg(u: str = Query(default="")):
    payload = await _on_air_payload()
    if not payload or u != payload["originalVideoUrl"]:
        return Response(status_code=404)
    return Response(_qr_svg(u), media_type="image/svg+xml")


def _qr_svg(url: str) -> bytes:
    """SVG document with xmlns. svg_inline omits it and rejects xmldecl, so <img> stays blank."""
    buffer = io.BytesIO()
    segno.make(url, error="h", micro=False).save(
        buffer, kind="svg", scale=8, border=1, xmldecl=False, svgns=True, nl=False,
    )
    svg = buffer.getvalue()
    if b"viewBox" not in svg:
        match = re.search(br'width="(\d+)" height="(\d+)"', svg)
        if match:
            width, height = match.group(1), match.group(2)
            svg = svg.replace(b"<svg ", f'<svg viewBox="0 0 {width.decode()} {height.decode()}" '.encode(), 1)
    svg = re.sub(br'width="\d+"', b'width="100%"', svg, count=1)
    svg = re.sub(br'height="\d+"', b'height="100%"', svg, count=1)
    return svg
