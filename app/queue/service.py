"""Queue service — scan channels via Invidious, filter eligible videos, populate queue."""
from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable

from app.database import get_db
from app.queue import download as queue_download
from app.yt import invidious
from app.yt.invidious import InvidiousError

log = logging.getLogger(__name__)


def _instance_down(exc: InvidiousError) -> bool:
    text = str(exc).lower()
    return "indisponível" in text or "http 5" in text

# One maintenance at a time. Callers of the same kind share that run's result.
_maintain_lock = asyncio.Lock()
_maintain_current: tuple[str, asyncio.Task] | None = None


async def scan_all_channels() -> dict[str, int]:
    """Scan every active channel and insert eligible videos. Returns stats."""
    added = 0
    skipped = 0
    errors = 0

    async with get_db() as db:
        cursor = await db.execute(
            "SELECT id, name, channel_id, min_age_hours FROM channels WHERE active = 1"
        )
        channels = [dict(row) for row in await cursor.fetchall()]

    for ch in channels:
        try:
            a, s = await _scan_channel(ch["channel_id"], ch["min_age_hours"], ch["name"])
            added += a
            skipped += s
        except InvidiousError as exc:
            errors += 1
            if _instance_down(exc):
                log.warning("Invidious indisponível; scan interrompido: %s", exc)
                return {
                    "added": added,
                    "skipped": skipped,
                    "errors": errors,
                    "error": "Invidious indisponível. Nenhum canal foi consultado depois deste.",
                }
            log.warning("Canal %s não consultado: %s", ch["name"], exc)
        except Exception:
            log.exception("Error scanning channel %s (%s)", ch["name"], ch["channel_id"])
            errors += 1

    log.info("Scan complete: %d added, %d skipped, %d errors", added, skipped, errors)
    return {"added": added, "skipped": skipped, "errors": errors}


async def _scan_channel(
    channel_id: str, min_age_hours: float, channel_name: str = "",
) -> tuple[int, int]:
    """Scan one channel. Returns (added, skipped)."""
    videos = await invidious.channel_videos(channel_id, sort_by="newest")
    shorts_ids = await invidious.channel_shorts_ids(channel_id)

    added = 0
    skipped = 0
    now = int(time.time())

    async with get_db() as db:
        for raw in videos:
            meta = invidious.normalize_video(raw)
            eligible, reason = invidious.is_eligible(
                meta, min_age_hours, shorts_ids=shorts_ids, now_unix=now,
            )
            if not eligible:
                skipped += 1
                continue
            meta["title"] = await invidious.video_title(meta["video_id"], meta["title"])

            try:
                cursor = await db.execute(
                    """INSERT OR IGNORE INTO queue
                       (channel_id, video_id, title, author, duration, published_unix, thumb)
                       SELECT id, ?, ?, ?, ?, ?, ?
                       FROM channels WHERE channel_id = ?
                       LIMIT 1""",
                    (
                        meta["video_id"],
                        meta["title"],
                        meta["author"],
                        meta["duracao"],
                        meta["published"] or 0,
                        meta["thumb"],
                        channel_id,
                    ),
                )
                inserted = bool(cursor.rowcount)
                # Refresh title and thumb on videos already queued.
                await db.execute(
                    "UPDATE queue SET title = ?, thumb = ? WHERE video_id = ?",
                    (meta["title"], meta["thumb"], meta["video_id"]),
                )
                if inserted:
                    added += 1
                else:
                    skipped += 1
            except Exception:
                log.debug("Duplicate or error for %s", meta.get("video_id"))
                skipped += 1

        await db.commit()

    return added, skipped


# Before the first shuffle every play_order is 0 and the table stays
# pending-first, then published time. After a shuffle, play_order is unique.
_QUEUE_ORDER = """
ORDER BY
    CASE WHEN (SELECT COALESCE(MAX(play_order), 0) FROM queue) > 0 THEN q.play_order ELSE 0 END ASC,
    CASE WHEN (SELECT COALESCE(MAX(play_order), 0) FROM queue) > 0 THEN 0
         ELSE CASE WHEN q.status = 'pending' THEN 1 ELSE 0 END END DESC,
    q.published_unix ASC,
    q.id ASC
"""


async def get_queue(status: str = "pending") -> list[dict]:
    """Get queue items in the same order as the full table."""
    async with get_db() as db:
        cursor = await db.execute(
            f"""SELECT q.id, q.video_id, q.title, q.author, q.duration,
                      q.published_unix, q.thumb, q.status, q.added_at,
                      q.local_path, q.download_error, q.play_order,
                      c.name as channel_name, c.channel_id
               FROM queue q
               JOIN channels c ON c.id = q.channel_id
               WHERE q.status = ?
               {_QUEUE_ORDER}""",
            (status,),
        )
        return [_with_thumb(row) for row in await cursor.fetchall()]


async def get_queue_full() -> list[dict]:
    """Get all queue items regardless of status."""
    async with get_db() as db:
        cursor = await db.execute(
            f"""SELECT q.id, q.video_id, q.title, q.author, q.duration,
                      q.published_unix, q.thumb, q.status, q.added_at,
                      q.local_path, q.download_error, q.play_order,
                      c.name as channel_name, c.channel_id
               FROM queue q
               JOIN channels c ON c.id = q.channel_id
               {_QUEUE_ORDER}"""
        )
        return [_with_thumb(row) for row in await cursor.fetchall()]


def _with_thumb(row) -> dict:
    item = dict(row)
    item["thumb"] = invidious.absolute_thumb(item.get("video_id") or "", item.get("thumb"))
    return item


async def mark_status(queue_id: int, status: str) -> bool:
    """Update queue item status (playing, played, expired)."""
    async with get_db() as db:
        await db.execute(
            "UPDATE queue SET status = ? WHERE id = ?",
            (status, queue_id),
        )
        await db.commit()
    queue_download.schedule_sync()
    return True


async def clear_queue_and_rescan() -> dict:
    """Delete every queue row and its files, then scan the channels again."""
    async with get_db() as db:
        cursor = await db.execute("SELECT video_id, local_path FROM queue")
        rows = [dict(row) for row in await cursor.fetchall()]
        await db.execute("DELETE FROM queue")
        await db.commit()
    queue_download.protect_video_ids(set())
    for row in rows:
        queue_download.discard_video_files(row["video_id"], row["local_path"] or "")
    stats = await maintain_queue()
    stats["cleared"] = len(rows)
    return stats


async def remove_from_queue(queue_id: int) -> bool:
    """Delete a queue item."""
    async with get_db() as db:
        await db.execute("DELETE FROM queue WHERE id = ?", (queue_id,))
        await db.commit()
    queue_download.schedule_sync()
    return True


async def get_next_video() -> dict | None:
    """Get the next pending video (oldest published first)."""
    videos = await get_queue(status="pending")
    return videos[0] if videos else None


async def _run_maintenance(key: str, factory: Callable[[], Awaitable[dict]]) -> dict:
    """Run factory, or share the in-flight run when key matches."""
    global _maintain_current
    while True:
        async with _maintain_lock:
            current = _maintain_current
            if current is None or current[1].done():
                task = asyncio.create_task(factory())
                _maintain_current = (key, task)
                break
            if current[0] == key:
                task = current[1]
                break
            other = current[1]
        await other
    return await task


async def maintain_queue() -> dict[str, int]:
    """Scan every active channel, then expire videos older than 24h."""
    async def _body() -> dict[str, int]:
        stats = await scan_all_channels()
        stats["expired"] = await expire_old_videos()
        queue_download.schedule_sync()
        return stats

    return await _run_maintenance("all", _body)


async def maintain_channel(
    channel_id: str, min_age_hours: float, channel_name: str = "",
) -> dict[str, int]:
    """Scan one channel, then expire videos older than 24h."""
    async def _body() -> dict[str, int]:
        added = skipped = errors = 0
        error = ""
        try:
            added, skipped = await _scan_channel(channel_id, min_age_hours, channel_name)
        except InvidiousError as exc:
            errors = 1
            if _instance_down(exc):
                log.warning("Invidious indisponível; canal %s não consultado: %s", channel_name, exc)
                error = "Invidious indisponível. O canal não foi consultado."
            else:
                log.warning("Canal %s não consultado: %s", channel_name, exc)
                error = str(exc)
        except Exception:
            log.exception("Error scanning channel %s (%s)", channel_name, channel_id)
            errors = 1
        stats: dict = {
            "added": added,
            "skipped": skipped,
            "errors": errors,
            "expired": await expire_old_videos(),
        }
        if error:
            stats["error"] = error
        return stats

    stats = await _run_maintenance(f"channel:{channel_id}", _body)
    queue_download.schedule_sync()
    return stats


async def expire_old_videos() -> int:
    """Remove pending videos older than 24h, and any row already marked expired."""
    return await queue_download.purge_expired()


async def shuffle_play_order() -> int:
    """Shuffle play_order for all queue items. Returns count of items shuffled."""
    import random
    async with get_db() as db:
        cursor = await db.execute("SELECT id FROM queue")
        rows = await cursor.fetchall()
        ids = [row["id"] for row in rows]
        if not ids:
            return 0
        random.shuffle(ids)
        for i, queue_id in enumerate(ids, 1):
            await db.execute("UPDATE queue SET play_order = ? WHERE id = ?", (i, queue_id))
        await db.commit()
        return len(ids)



