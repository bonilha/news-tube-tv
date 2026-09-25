"""Lower-third lines. The page and OBS text sources get these strings; nothing is baked into art."""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

_TZ = ZoneInfo("America/Sao_Paulo")


def channel_label(handle: str, author: str) -> str:
    text = (handle or "").strip()
    if text:
        return text if text.startswith("@") else f"@{text}"
    return (author or "").strip()


def youtube_url(video_id: str) -> str:
    return f"https://www.youtube.com/watch?v={video_id}"


def published_line(published_unix: int, now: datetime | None = None) -> str:
    """`13/09/2026 • 16:30 - Publicado há 15 horas` in America/Sao_Paulo."""
    moment = datetime.fromtimestamp(int(published_unix), _TZ)
    clock = now.astimezone(_TZ) if now is not None else datetime.now(_TZ)
    stamp = moment.strftime("%d/%m/%Y • %H:%M")
    seconds = int((clock - moment).total_seconds())
    if seconds < 0:
        seconds = 0
    if seconds < 60:
        relative = "Publicado há menos de 1 minuto"
    elif seconds < 3600:
        count = seconds // 60
        relative = f"Publicado há {count} minuto" + ("s" if count != 1 else "")
    elif seconds < 86400:
        count = seconds // 3600
        relative = f"Publicado há {count} hora" + ("s" if count != 1 else "")
    else:
        count = seconds // 86400
        relative = f"Publicado há {count} dia" + ("s" if count != 1 else "")
    return f"{stamp} - {relative}"
