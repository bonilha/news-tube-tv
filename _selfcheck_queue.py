"""Self-check: the failed video must not occupy the window, its error must
survive the sync, and the retry only happens when its turn comes round.

Run:  python _selfcheck_queue.py
"""
import asyncio
import os
import time

from app.config import settings
from app.database import get_db, init_db
from app.queue import download as dl

DB = "_selfcheck.db"
settings.DB_PATH = DB

READY = "bbb22222222"
FAILED = "aaa11111111"
ALSOF = "ccc33333333"


async def main() -> None:
    if os.path.exists(DB):
        os.remove(DB)
    await init_db()
    now = int(time.time())
    async with get_db() as db:
        await db.execute("INSERT INTO channels (id, name, channel_id) VALUES (1, 'c', 'UC1')")
        rows = [
            (1, FAILED, 1, "", "HTTP Error 403"),
            (2, READY, 2, "", ""),
            (3, ALSOF, 3, "", "yt-dlp did not produce an MP4"),
        ]
        for queue_id, video_id, order, path, err in rows:
            await db.execute(
                "INSERT INTO queue (id, channel_id, video_id, title, author, duration,"
                " published_unix, status, play_order, local_path, download_error)"
                " VALUES (?, ?, ?, ?, 'a', 60, ?, 'pending', ?, ?, ?)",
                (queue_id, 1, video_id, video_id, now, order, path, err),
            )
        await db.commit()

    rows = await dl._queue_rows()
    window = dl.buffer_ids(rows, 5, set())
    assert FAILED not in window, window
    assert ALSOF not in window, window
    assert window == [READY], window

    # The sync forgets paths outside the window but must keep the errors, or the
    # failed video becomes downloadable again and parks the cycle on it.
    await dl._forget_paths_outside(dl.retain_ids(rows, 5, set(), ""))
    errors = {row["video_id"]: row["download_error"] for row in await dl._queue_rows()}
    assert errors[FAILED] == "HTTP Error 403", errors
    assert errors[ALSOF] == "yt-dlp did not produce an MP4", errors

    # Only when the pass wraps does the error clear, and only for what is
    # otherwise still eligible.
    retried = await dl.retry_error_videos()
    assert retried == 2, retried
    errors = {row["video_id"]: row["download_error"] for row in await dl._queue_rows()}
    assert errors[FAILED] == "" and errors[ALSOF] == "", errors

    stale = now - 90000
    async with get_db() as db:
        await db.execute(
            "UPDATE queue SET download_error = 'boom', published_unix = ? WHERE video_id = ?",
            (stale, ALSOF),
        )
        await db.commit()
    assert await dl.retry_error_videos() == 0
    os.remove(DB)
    print("queue self-check ok")


asyncio.run(main())
