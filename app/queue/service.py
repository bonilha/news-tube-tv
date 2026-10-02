"""Queue service — scan channels via Invidious, filter eligible videos, populate queue."""
from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from pathlib import Path

from app.config import settings
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
        cursor = await db.execute("SELECT COALESCE(MAX(play_order), 0) FROM queue")
        next_order = int((await cursor.fetchone())[0] or 0)
        for raw in videos:
            meta = invidious.normalize_video(raw)
            eligible, reason = invidious.is_eligible(
                meta, min_age_hours, shorts_ids=shorts_ids, now_unix=now,
            )
            if not eligible:
                skipped += 1
                continue
            meta["title"], meta["max_height"] = await invidious.video_title(
                meta["video_id"], meta["title"],
            )

            try:
                play_order = next_order + 1 if next_order > 0 else 0
                cursor = await db.execute(
                    """INSERT OR IGNORE INTO queue
                       (channel_id, video_id, title, author, duration, published_unix, thumb, play_order, max_height)
                       SELECT id, ?, ?, ?, ?, ?, ?, ?, ?
                       FROM channels WHERE channel_id = ?
                       LIMIT 1""",
                    (
                        meta["video_id"],
                        meta["title"],
                        meta["author"],
                        meta["duracao"],
                        meta["published"] or 0,
                        meta["thumb"],
                        play_order,
                        int(meta.get("max_height") or 0),
                        channel_id,
                    ),
                )
                if cursor.rowcount:
                    next_order = play_order
                inserted = bool(cursor.rowcount)
                # Refresh title and thumb on videos already queued.
                await db.execute(
                    "UPDATE queue SET title = ?, thumb = ?, max_height = ? WHERE video_id = ?",
                    (meta["title"], meta["thumb"], int(meta.get("max_height") or 0), meta["video_id"]),
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
                      q.local_path, q.download_error, q.play_order, q.play_count,
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
                      q.local_path, q.download_error, q.play_order, q.play_count,
                      c.name as channel_name, c.channel_id
               FROM queue q
               JOIN channels c ON c.id = q.channel_id
               {_QUEUE_ORDER}"""
        )
        return [_with_thumb(row) for row in await cursor.fetchall()]


def _with_thumb(row) -> dict:
    item = dict(row)
    item["thumb"] = invidious.absolute_thumb(item.get("video_id") or "", item.get("thumb"))
    path = item.get("local_path") or ""
    if path and not Path(path).is_file():
        item["local_path"] = ""
    return item


async def mark_status(queue_id: int, status: str) -> bool:
    """Update queue item status (playing, played, expired)."""
    async with get_db() as db:
        if status == "played":
            await db.execute(
                "UPDATE queue SET status = ?, play_count = play_count + 1 WHERE id = ?",
                (status, queue_id),
            )
        else:
            await db.execute(
                "UPDATE queue SET status = ? WHERE id = ?",
                (status, queue_id),
            )
        await db.commit()
    queue_download.schedule_sync()
    return True


async def mark_status_by_video_id(video_id: str, status: str) -> bool:
    """Update queue item status by video_id."""
    async with get_db() as db:
        if status == "played":
            await db.execute(
                "UPDATE queue SET status = ?, play_count = play_count + 1 WHERE video_id = ?",
                (status, video_id),
            )
        else:
            await db.execute(
                "UPDATE queue SET status = ? WHERE video_id = ?",
                (status, video_id),
            )
        await db.commit()
    return True


async def rotate_video_to_end(video_id: str) -> None:
    """Move a finished video to the end of the play_order queue (round-robin).

    Increments play_count, resets status to 'pending', and assigns the highest
    play_order so the video goes to the back of the line.
    """
    async with get_db() as db:
        cursor = await db.execute("SELECT COALESCE(MAX(play_order), 0) FROM queue")
        row = await cursor.fetchone()
        max_order = row[0] if row else 0
        await db.execute(
            "UPDATE queue SET play_order = ?, status = 'pending', play_count = play_count + 1 "
            "WHERE video_id = ?",
            (max_order + 1, video_id),
        )
        await db.commit()
    queue_download.schedule_sync()


async def reset_played_to_pending() -> int:
    """Reset played videos back to pending for the next cycle round."""
    async with get_db() as db:
        cursor = await db.execute(
            "UPDATE queue SET status = 'pending' WHERE status = 'played'"
        )
        count = cursor.rowcount
        await db.commit()
    return count


async def clear_queue_and_rescan() -> dict:
    """Delete every queue row and its files, then scan and shuffle before downloads."""
    queue_download.hold_downloads()
    try:
        async with get_db() as db:
            cursor = await db.execute("SELECT video_id, local_path FROM queue")
            rows = [dict(row) for row in await cursor.fetchall()]
            await db.execute("DELETE FROM queue")
            await db.commit()
        queue_download.protect_video_ids(set())
        for row in rows:
            await queue_download.discard_video_files(row["video_id"], row["local_path"] or "")
        stats = await scan_all_channels()
        stats["expired"] = await expire_old_videos()
        stats["cleared"] = len(rows)
        # Scan inserts play_order 0, so the table would show oldest-first and a
        # 720p can lead. Shuffle before the download window is filled.
        stats["shuffled"] = await shuffle_play_order(reset_files=False)
        return stats
    finally:
        queue_download.release_downloads()


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
        try:
            await other
        except Exception:
            # Another scan failed. That is not this call's failure; loop and
            # start our own run instead of sharing their error.
            pass
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


def has_video_file(row: dict) -> bool:
    path = row.get("local_path") or ""
    return bool(path) and Path(path).is_file()


async def first_broadcast_row() -> dict | None:
    """First pending item in the queue the broadcast must play."""
    for row in await get_queue_full():
        if row.get("status") in ("pending", "playing"):
            return row
    return None


async def broadcast_ready() -> bool:
    """True when some queued video already has an MP4 the cycle can play."""
    for row in await get_queue_full():
        if has_video_file(row):
            return True
    return False


async def startup_broadcast_queue() -> dict[str, int]:
    """Wipe MP4s, scan, shuffle, and only then download the new order.

    Lifespan may already be holding the window so the buffer loop cannot fill
    it before this task runs. One release opens it, after the shuffle.
    """
    if not queue_download.downloads_held():
        queue_download.hold_downloads()
    try:
        await queue_download.discard_all_downloads()
        stats = await scan_all_channels()
        stats["expired"] = await expire_old_videos()
        stats["shuffled"] = await shuffle_play_order(reset_files=False)
        return stats
    finally:
        queue_download.release_downloads()


_LOW_RES_HEIGHT = 720


def shuffle_ids(rows: list[tuple[int, int, int, int]], head: int) -> list[int]:
    """Queue ids with the same channel apart, and low resolution after `head`.

    Each row is (queue id, channel id, max height, play count). A known height
    of 720 or less follows every taller or unknown video, so the first `head`
    positions stay clear of it when enough taller videos exist. Inside each of
    those groups, never-aired videos come first, then one play, then two, and
    so on. Each play-count band is spread on its own.
    """
    del head  # the split is the whole taller group, which covers the first N

    def band(selected: list[tuple[int, int, int]]) -> list[int]:
        counts = sorted({play_count for _queue_id, _channel_id, play_count in selected})
        ordered: list[int] = []
        for play_count in counts:
            group = [
                (queue_id, channel_id)
                for queue_id, channel_id, count in selected
                if count == play_count
            ]
            ordered.extend(spread_play_order(group))
        return ordered

    low = [
        (queue_id, channel_id, int(play_count))
        for queue_id, channel_id, height, play_count in rows
        if 0 < int(height) <= _LOW_RES_HEIGHT
    ]
    taller = [
        (queue_id, channel_id, int(play_count))
        for queue_id, channel_id, height, play_count in rows
        if not (0 < int(height) <= _LOW_RES_HEIGHT)
    ]
    return band(taller) + band(low)


def spread_play_order(rows: list[tuple[int, int]]) -> list[int]:
    """Queue ids with the same channel kept apart when the counts allow it.

    No two from one channel sit together when that is possible. When one
    channel has too many videos, the extras are spread so a run stays as
    short as the rest of the queue allows.
    """
    import random

    by_channel: dict[int, list[int]] = {}
    for queue_id, channel_id in rows:
        by_channel.setdefault(channel_id, []).append(queue_id)
    for ids in by_channel.values():
        random.shuffle(ids)

    remaining = {channel_id: len(ids) for channel_id, ids in by_channel.items()}
    sequence: list[int] = []
    while sum(remaining.values()):
        last = sequence[-1] if sequence else None
        choices = [channel_id for channel_id, count in remaining.items() if count and channel_id != last]
        if not choices:
            break
        most = max(remaining[channel_id] for channel_id in choices)
        tied = [channel_id for channel_id in choices if remaining[channel_id] == most]
        pick = random.choice(tied)
        sequence.append(pick)
        remaining[pick] -= 1

    leftover = next((channel_id for channel_id, count in remaining.items() if count), None)
    if leftover is not None:
        sequence = _insert_channel(sequence, leftover, remaining[leftover])

    ordered: list[int] = []
    for channel_id in sequence:
        ordered.append(by_channel[channel_id].pop())
    return ordered


def _run_after_insert(sequence: list[int], index: int, channel_id: int) -> int:
    left = 0
    cursor = index - 1
    while cursor >= 0 and sequence[cursor] == channel_id:
        left += 1
        cursor -= 1
    right = 0
    cursor = index
    while cursor < len(sequence) and sequence[cursor] == channel_id:
        right += 1
        cursor += 1
    return left + 1 + right


def _insert_channel(sequence: list[int], channel_id: int, extra: int) -> list[int]:
    """Place leftover videos of one channel into the gaps that form the shortest run."""
    import random

    result = list(sequence)
    for _ in range(extra):
        scored = [
            (_run_after_insert(result, index, channel_id), index)
            for index in range(len(result) + 1)
        ]
        shortest = min(score for score, _index in scored)
        chosen = random.choice([index for score, index in scored if score == shortest])
        result.insert(chosen, channel_id)
    return result


async def shuffle_play_order(*, reset_files: bool = False) -> int:
    """Shuffle play_order for all queue items. Returns count of items shuffled."""
    async with get_db() as db:
        cursor = await db.execute(
            "SELECT id, channel_id, max_height, play_count FROM queue"
        )
        rows = [
            (
                row["id"],
                row["channel_id"],
                int(row["max_height"] or 0),
                int(row["play_count"] or 0),
            )
            for row in await cursor.fetchall()
        ]
        if not rows:
            return 0
        ordered = shuffle_ids(rows, settings.QUEUE_DOWNLOAD_KEEP)
        for position, queue_id in enumerate(ordered, 1):
            await db.execute("UPDATE queue SET play_order = ? WHERE id = ?", (position, queue_id))
        await db.commit()
    if reset_files:
        await queue_download.discard_all_downloads()
    return len(rows)



