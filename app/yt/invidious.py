"""Async Invidious API client — ported from x-live for news-tube-tv."""
from __future__ import annotations

import logging
import re
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


def _listed_quality(item: dict[str, Any]) -> int:
    """Short side of one stream, which is the 720p/1080p number."""
    width = height = 0
    try:
        width = int(item.get("width") or 0)
        height = int(item.get("height") or 0)
    except (TypeError, ValueError):
        width = height = 0
    if width <= 0 or height <= 0:
        size = str(item.get("size") or "")
        if "x" in size:
            left, _, right = size.lower().partition("x")
            try:
                width, height = int(left), int(right)
            except ValueError:
                width = height = 0
    if width > 0 and height > 0:
        return min(width, height)
    if height > 0:
        return height
    if width > 0:
        return width
    for key in ("qualityLabel", "resolution"):
        digits = "".join(ch if ch.isdigit() else " " for ch in str(item.get(key) or "")).split()
        if digits:
            return int(digits[0])
    return 0


def stream_max_height(data: dict[str, Any]) -> int:
    """Best listed quality (720, 1080, …). Zero when the payload has none.

    This Invidious build puts the frame in `size` (`1280x720`), not `height`.
    """
    qualities = [
        _listed_quality(item)
        for key in ("adaptiveFormats", "formatStreams")
        for item in (data.get(key) or [])
        if isinstance(item, dict)
    ]
    qualities = [value for value in qualities if value > 0]
    return max(qualities) if qualities else 0


_RELATIVE_AGE = re.compile(
    r"(?:há\s+)?(\d+)\s+"
    r"(segundos?|minutos?|horas?|dias?|semanas?|m[eê]ses|m[eê]s|anos?"
    r"|seconds?|minutes?|hours?|days?|weeks?|months?|years?)"
    r"(?:\s+atr[aá]s|\s+ago)?",
    re.IGNORECASE,
)
_AGE_SECONDS = {
    "segundo": 1,
    "minute": 60,
    "minuto": 60,
    "hora": 3600,
    "hour": 3600,
    "dia": 86400,
    "day": 86400,
    "semana": 7 * 86400,
    "week": 7 * 86400,
    "mes": 30 * 86400,
    "mês": 30 * 86400,
    "month": 30 * 86400,
    "ano": 365 * 86400,
    "year": 365 * 86400,
}


def _unit_seconds(unit: str) -> int:
    key = unit.lower()
    if key.startswith("mês") or key.startswith("mes"):
        return 30 * 86400
    for name, seconds in _AGE_SECONDS.items():
        if key.startswith(name):
            return seconds
    return 0


def relative_age_seconds(text: str | None) -> int | None:
    """Seconds ago from a relative publishedText, or None when it is not relative."""
    if not text:
        return None
    match = _RELATIVE_AGE.search(text.strip())
    if not match:
        return None
    seconds = _unit_seconds(match.group(2))
    if seconds <= 0:
        return None
    return int(match.group(1)) * seconds


EXACT = "exact"
APPROX = "approx"
UNKNOWN = "unknown"
# A date without a time can name the day before the real publish moment. One
# extra day of room keeps a video that is only hours old from being dropped.
APPROX_EXTRA_HOURS = 24


def is_midnight(published_unix: int | None) -> bool:
    """True when the stamp names a whole UTC day instead of a moment.

    Invidious rounds some channels to 00:00:00, and a real publish time is never
    exactly on the hour, so the modulo is a safe tell.
    """
    return bool(published_unix) and int(published_unix) % 86400 == 0


def publish_confidence(
    published_unix: int | None, payload: dict[str, Any] | None = None,
) -> str:
    """How much the stored publish time can be trusted."""
    age = relative_age_seconds((payload or {}).get("publishedText"))
    if age is not None and age > 0 and age % 86400 == 0:
        # The text only counted whole days, so the moment is a lower bound.
        return APPROX
    if not published_unix:
        return UNKNOWN
    if is_midnight(published_unix):
        return APPROX
    return EXACT


def window_hours(
    confidence: str, max_age_hours: int = 24, approx_extra: int = APPROX_EXTRA_HOURS,
) -> int:
    """Age limit for a stamp: one day, or two while it is only a date."""
    return max_age_hours + (approx_extra if confidence == APPROX else 0)


def resolve_published(
    published_unix: int | None,
    published_text: str | None,
    now_unix: int,
) -> int | None:
    """Unix time for the 24h window.

    Invidious often stores only UTC midnight in `published` while `publishedText`
    still says "16 horas atrás". A midnight stamp must not replace that age.
    A real timestamp inside 24h is kept when the text only says "1 dia atrás".
    A real timestamp is also kept when the text is not relative.
    A midnight stamp is kept as the day it names: the caller judges it as
    approximate instead of throwing the video away.
    """
    age = relative_age_seconds(published_text)
    # "1 dia atrás" is a whole day. It must not replace a real stamp still inside 24h.
    known_fresh = (
        published_unix is not None
        and not is_midnight(published_unix)
        and 0 <= (now_unix - int(published_unix)) < 86400
    )
    if age is not None:
        if known_fresh and age >= 86400:
            return int(published_unix)
        return now_unix - age
    if published_unix is None:
        return None
    return int(published_unix)


def members_only(payload: dict[str, Any] | None) -> bool:
    """True when the video-detail payload says the video is for channel members.

    The channel list does not include this. GET /videos/:id puts the YouTube
    playability reason in `error` ("Join this channel… members-only…").
    """
    if not payload:
        return False
    error = str(payload.get("error") or "")
    return "member" in error.lower()


async def video_title(
    video_id: str, fallback: str, defer_published: bool = False,
) -> tuple[str, int, bool | None, int | None, int]:
    """Title, height, members-only, published unix, and length from the video endpoint.

    The channel list ignores hl and sometimes stamps published as "now".
    GET /videos/:id often puts only UTC midnight in `published` and the real
    age in `publishedText` ("16 horas atrás"). That midnight is not returned.
    Members-only answers HTTP 500 with only `error`, which `_get` would throw away.
    Members is None when the request fails for another reason.
    Published is None when the payload has no usable time. defer_published keeps
    the date-only stamp instead of dropping it, for a shelf that sent no time at all.
    """
    if not video_id:
        return fallback, 0, None, None, 0
    url = f"{_api_url()}/videos/{video_id}"
    try:
        async with httpx.AsyncClient(timeout=25.0, follow_redirects=True) as client:
            resp = await client.get(url, params={"hl": content_language()})
            data = resp.json()
    except (httpx.HTTPError, ValueError):
        return fallback, 0, None, None, 0
    if not isinstance(data, dict):
        return fallback, 0, None, None, 0
    if members_only(data):
        return fallback, 0, True, None, 0
    if resp.status_code >= 400 or data.get("error"):
        return fallback, 0, None, None, 0
    title = (data.get("title") or "").strip() or fallback
    published = data.get("published")
    try:
        published_unix = int(published) if published else None
    except (TypeError, ValueError):
        published_unix = None
    try:
        length = int(data.get("lengthSeconds") or 0)
    except (TypeError, ValueError):
        length = 0
    import time as _time

    resolved = resolve_published(published_unix, data.get("publishedText"), int(_time.time()))
    if resolved is None and defer_published and published_unix:
        # A shelf item has no time of its own. A date-only stamp beats no date.
        resolved = int(published_unix)
    return title, stream_max_height(data), False, resolved, length


async def channel_videos(ucid: str, sort_by: str = "newest") -> list[dict[str, Any]]:
    from urllib.parse import quote
    data = await _get(f"/channels/{quote(ucid, safe='')}/videos", {"sort_by": sort_by})
    if isinstance(data, dict):
        return list(data.get("videos") or data.get("latestVideos") or [])
    if isinstance(data, list):
        return data
    return []


async def channel_shorts(ucid: str, sort_by: str = "newest") -> list[dict[str, Any]]:
    from urllib.parse import quote
    try:
        data = await _get(f"/channels/{quote(ucid, safe='')}/shorts", {"sort_by": sort_by})
    except InvidiousError:
        return []
    if isinstance(data, dict):
        return list(data.get("videos") or data.get("latestVideos") or [])
    if isinstance(data, list):
        return data
    return []


async def channel_shorts_ids(ucid: str) -> set[str]:
    videos = await channel_shorts(ucid)
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
    max_duration_minutes: int | float | None = None,
    allow_shorts: bool = False,
    defer_age: bool = False,
    age_confidence: str | None = None,
) -> tuple[bool, str]:
    """Check if a video is eligible for the queue. Returns (eligible, reason).

    defer_age skips the publish-time checks. The shorts shelf sends published 0,
    and the scan fills the real unix time from the video endpoint before judging age.
    A date-only or day-rounded stamp is judged one day wider, and never dropped
    for being "new": Invidious answers 00:00:00 for those, which is the start of
    the day, not the publish moment.
    """
    import time as _time

    classification = classify_video(meta, shorts_ids)
    if classification == "short":
        if not allow_shorts:
            return False, "Excluído: short"
    elif classification != "video":
        return False, f"Excluído: {classification}"

    length = int(meta.get("duracao") or meta.get("lengthSeconds") or 0)
    if length <= 0:
        return False, "Duração inválida"

    max_minutes = max_duration_minutes if max_duration_minutes is not None else settings.QUEUE_MAX_DURATION_MINUTES
    if max_minutes > 0 and length > (max_minutes * 60):
        return False, f"Duração longa demais ({length // 60}m > {max_minutes}m)"

    published = meta.get("publicacao_unix") or meta.get("published")
    confidence = age_confidence or publish_confidence(published)
    if not defer_age:
        if not published:
            return False, "Sem data de publicação"

        now = now_unix if now_unix is not None else int(_time.time())
        age_hours = (now - int(published)) / 3600.0

        # A date-only stamp names the start of the day, so it can only look older
        # than the video is. Never drop it for being new, and judge the stale end
        # one day wider.
        if age_hours < min_age_hours and confidence == EXACT:
            return False, f"Novo demais ({age_hours:.1f}h < {min_age_hours:.1f}h)"

        if age_hours >= window_hours(confidence):
            return False, f"Fora das 24h ({age_hours:.1f}h)"

    paid = meta.get("paid") or meta.get("premium")
    if paid:
        return False, "Conteúdo pago/premium"

    if meta.get("is_members") or members_only(meta):
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
        "published_confidence": publish_confidence(published_unix, payload),
        "duracao": length,
        "type": str(payload.get("type") or "video").lower(),
        "liveNow": bool(payload.get("liveNow")),
        "isUpcoming": bool(payload.get("isUpcoming")),
        "premiereTimestamp": payload.get("premiereTimestamp"),
        "paid": bool(payload.get("paid") or payload.get("premium")),
        "isListed": payload.get("isListed", True),
        "is_members": members_only(payload),
        "thumb": thumb,
        "error": payload.get("error"),
    }
