"""Exact publish times from the YouTube Data API v3.

One videos.list call accepts up to 50 ids and costs one quota unit.
Any failure returns an empty map so the scan can keep Invidious times.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

import httpx

from app.config import settings

log = logging.getLogger(__name__)

_VIDEOS_URL = "https://www.googleapis.com/youtube/v3/videos"
_BATCH = 50


def _published_unix(value: str) -> int | None:
    text = (value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return int(moment.timestamp())


async def fetch_videos_published_at(video_ids: list[str]) -> dict[str, int]:
    """Map video id to the exact publishedAt unix time.

    Returns {} when the key is missing or any request fails, so the caller
    can fall back to Invidious.
    """
    key = (settings.YOUTUBE_API_KEY or "").strip()
    ids = [vid for vid in dict.fromkeys(video_ids) if vid]
    if not key or not ids:
        return {}
    found: dict[str, int] = {}
    try:
        async with httpx.AsyncClient(timeout=25.0) as client:
            for start in range(0, len(ids), _BATCH):
                batch = ids[start:start + _BATCH]
                resp = await client.get(
                    _VIDEOS_URL,
                    params={"part": "snippet", "id": ",".join(batch), "key": key},
                )
                resp.raise_for_status()
                payload = resp.json()
                if not isinstance(payload, dict) or payload.get("error"):
                    log.warning("YouTube Data API recusou o lote de datas: %s", payload)
                    return {}
                for item in payload.get("items") or []:
                    video_id = str(item.get("id") or "")
                    snippet = item.get("snippet") or {}
                    stamp = _published_unix(str(snippet.get("publishedAt") or ""))
                    if video_id and stamp is not None:
                        found[video_id] = stamp
    except (httpx.HTTPError, ValueError, TypeError) as exc:
        log.warning("YouTube Data API indisponível, datas ficam no Invidious: %s", exc)
        return {}
    return found
