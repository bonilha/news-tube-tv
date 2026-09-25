"""Async Invidious API client — ported from x-live for news-tube-tv."""
from __future__ import annotations

import logging
from typing import Any

import httpx

from app.config import settings

log = logging.getLogger(__name__)

SHORT_MIN_SECONDS = 60
LIVE_TYPES = {"livestream", "scheduled", "premiere", "live"}


class InvidiousError(Exception):
    pass


def _api_url() -> str:
    return f"{settings.INVIDIOUS_URL.rstrip('/')}/api/v1"


def absolute_thumb(video_id: str, url: str | None) -> str:
    """Turn an Invidious thumbnail into a URL the browser can load."""
    base = settings.INVIDIOUS_URL.rstrip("/")
    raw = (url or "").strip()
    if not raw and video_id:
        raw = f"/vi/{video_id}/mqdefault.jpg"
    if raw.startswith("//"):
        return "https:" + raw
    if raw.startswith("/"):
        return base + raw
    return raw


def content_language() -> str:
    """YouTube hl uses a hyphen. pt_BR in the config is the same as pt-BR."""
    return (settings.CONTENT_LANGUAGE or "pt-BR").strip().replace("_", "-")


async def _get(path: str, params: dict | None = None, timeout: float = 25.0) -> Any:
    url = f"{_api_url()}{path}"
    query = {"hl": content_language()}
    if params:
        query.update(params)
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            resp = await client.get(url, params=query)
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPStatusError as e:
        raise InvidiousError(f"Invidious HTTP {e.response.status_code}") from e
    except httpx.HTTPError as e:
        raise InvidiousError(f"Invidious indisponível: {e}") from e
    if isinstance(data, dict) and data.get("error"):
        raise InvidiousError(str(data["error"]))
    return data


async def resolve_channel(url_or_handle: str) -> dict[str, Any]:
    """Resolve a YouTube handle/URL to channel info via Invidious."""
    raw = (url_or_handle or "").strip()
    if raw.startswith("UC") and len(raw) >= 22:
        return await get_channel(raw)
    if raw.startswith("@"):
        yt_url = f"https://www.youtube.com/{raw}"
    elif raw.startswith("http"):
        yt_url = raw
    else:
        yt_url = f"https://www.youtube.com/@{raw.lstrip('@')}"

    try:
        resolved = await _get("/resolveurl", {"url": yt_url})
        ucid = resolved.get("ucid") or resolved.get("browseId")
        if ucid:
            return await get_channel(ucid)
    except InvidiousError:
        pass

    handle = raw
    if "youtube.com/" in raw:
        handle = raw.rstrip("/").split("/")[-1]
    if not handle.startswith("@"):
        handle = "@" + handle.lstrip("@")
    try:
        return await get_channel(handle)
    except InvidiousError:
        pass
    try:
        results = await _get("/search", {"q": handle, "type": "channel"})
        if isinstance(results, list):
            for item in results:
                author = (item.get("author") or "").lower()
                author_url = (item.get("authorUrl") or "").lower()
                if handle.lower().lstrip("@") in author or handle.lower() in author_url:
                    return await get_channel(item["authorId"])
            if results and results[0].get("authorId"):
                return await get_channel(results[0]["authorId"])
    except InvidiousError:
        pass
    raise InvidiousError(f"Não foi possível resolver o canal {raw}")


async def get_channel(ucid: str) -> dict[str, Any]:
    from urllib.parse import quote
    return await _get(f"/channels/{quote(ucid, safe='')}")


async def video_title(video_id: str, fallback: str) -> str:
    """Title from the video endpoint, which honors hl. The channel list does not."""
    if not video_id:
        return fallback
    try:
        data = await _get(f"/videos/{video_id}")
    except InvidiousError:
        return fallback
    if not isinstance(data, dict):
        return fallback
    return (data.get("title") or "").strip() or fallback


async def channel_videos(ucid: str, sort_by: str = "newest") -> list[dict[str, Any]]:
    from urllib.parse import quote
    data = await _get(f"/channels/{quote(ucid, safe='')}/videos", {"sort_by": sort_by})
    if isinstance(data, dict):
        return list(data.get("videos") or data.get("latestVideos") or [])
    if isinstance(data, list):
        return data
    return []


async def channel_shorts_ids(ucid: str) -> set[str]:
    from urllib.parse import quote
    try:
        data = await _get(f"/channels/{quote(ucid, safe='')}/shorts", {"sort_by": "newest"})
    except InvidiousError:
        return set()
    videos = data.get("videos", []) if isinstance(data, dict) else (data if isinstance(data, list) else [])
    return {v.get("videoId") or v.get("videoID") for v in videos if v.get("videoId") or v.get("videoID")}


# ---------------------------------------------------------------------------
# Classification & eligibility (simplified from x-live eligibility.py)
# ---------------------------------------------------------------------------

def classify_video(meta: dict[str, Any], shorts_ids: set[str] | None = None) -> str:
    """Classify a video as 'video', 'short', 'live', 'premiere', or 'scheduled'."""
    video_id = str(meta.get("video_id") or meta.get("videoId") or "")
    length = int(meta.get("duracao") or meta.get("lengthSeconds") or 0)
    tipo = str(meta.get("tipo") or meta.get("type") or "video").lower()

    if tipo in LIVE_TYPES or meta.get("liveNow"):
        if meta.get("premiereTimestamp"):
            return "premiere"
        if meta.get("isUpcoming"):
            return "scheduled"
        return "live"
    if meta.get("premiereTimestamp"):
        return "premiere"
    if shorts_ids and video_id in shorts_ids:
        return "short"
    if 0 < length < SHORT_MIN_SECONDS:
        return "short"
    if "#short" in (meta.get("title") or "").lower():
        return "short"
    return "video"


def is_eligible(
    meta: dict[str, Any],
    min_age_hours: float,
    shorts_ids: set[str] | None = None,
    now_unix: int | None = None,
) -> tuple[bool, str]:
    """Check if a video is eligible for the queue. Returns (eligible, reason)."""
    import time as _time

    classification = classify_video(meta, shorts_ids)
    if classification != "video":
        return False, f"Excluído: {classification}"

    length = int(meta.get("duracao") or meta.get("lengthSeconds") or 0)
    if length <= 0:
        return False, "Duração inválida"

    published = meta.get("publicacao_unix") or meta.get("published")
    if published is None:
        return False, "Sem data de publicação"

    now = now_unix if now_unix is not None else int(_time.time())
    age_hours = (now - int(published)) / 3600.0

    if age_hours < min_age_hours:
        return False, f"Novo demais ({age_hours:.1f}h < {min_age_hours:.1f}h)"

    if age_hours >= 24:
        return False, f"Fora das 24h ({age_hours:.1f}h)"

    paid = meta.get("paid") or meta.get("premium")
    if paid:
        return False, "Conteúdo pago/premium"

    members = meta.get("is_members") or (
        meta.get("isListed") is False and meta.get("error") == "members"
    )
    if members:
        return False, "Conteúdo exclusivo para membros"

    return True, "ok"


def normalize_video(payload: dict[str, Any]) -> dict[str, Any]:
    """Normalize Invidious video payload to a flat dict."""
    published = payload.get("published")
    try:
        published_unix = int(published) if published is not None else None
    except (TypeError, ValueError):
        published_unix = None

    length = int(payload.get("lengthSeconds") or 0)
    thumbs = payload.get("videoThumbnails") or []
    thumb_raw = ""
    if thumbs:
        preferred = next(
            (t for t in thumbs if t.get("quality") in {"medium", "high", "sddefault", "mqdefault"}),
            thumbs[0],
        )
        thumb_raw = preferred.get("url") or ""
    video_id = payload.get("videoId") or ""
    thumb = absolute_thumb(video_id, thumb_raw)

    author_url = payload.get("authorUrl") or ""
    handle = ""
    if "/@" in author_url:
        handle = "@" + author_url.split("/@")[-1].strip("/")

    return {
        "video_id": video_id,
        "title": payload.get("title") or "",
        "author": payload.get("author") or "",
        "authorId": payload.get("authorId"),
        "authorUrl": author_url,
        "handle": handle,
        "published": published_unix,
        "duracao": length,
        "type": str(payload.get("type") or "video").lower(),
        "liveNow": bool(payload.get("liveNow")),
        "isUpcoming": bool(payload.get("isUpcoming")),
        "premiereTimestamp": payload.get("premiereTimestamp"),
        "paid": bool(payload.get("paid") or payload.get("premium")),
        "isListed": payload.get("isListed", True),
        "is_members": bool(payload.get("isFamilySafe") is False and "member" in str(payload.get("error") or "")),
        "thumb": thumb,
        "error": payload.get("error"),
    }
