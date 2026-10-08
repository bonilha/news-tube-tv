"""Channel scan must not hold SQLite while it asks Invidious for a title."""

import asyncio
import time
from contextlib import asynccontextmanager

from app.config import settings
from app.database import get_db, init_db
from app.queue import service as queue_svc


def test_video_detail_runs_outside_the_database(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "DB_PATH", tmp_path / "scan.db")
    open_depth = 0
    real = queue_svc.get_db

    @asynccontextmanager
    async def tracking():
        nonlocal open_depth
        open_depth += 1
        try:
            async with real() as db:
                yield db
        finally:
            open_depth -= 1

    async def videos(_channel_id, sort_by="newest"):
        now = int(time.time())
        return [{
            "videoId": "abcdefghijk",
            "title": "English list title",
            "author": "Canal",
            "lengthSeconds": 90,
            "published": now - 3600,
            "type": "video",
            "videoThumbnails": [],
        }]

    async def no_shorts(_channel_id):
        return set()

    async def detail(_video_id, _fallback):
        assert open_depth == 0
        return "Título em português", 1080, False

    monkeypatch.setattr(queue_svc, "get_db", tracking)
    monkeypatch.setattr(queue_svc.invidious, "channel_videos", videos)
    monkeypatch.setattr(queue_svc.invidious, "channel_shorts_ids", no_shorts)
    monkeypatch.setattr(queue_svc.invidious, "video_title", detail)

    async def run():
        await init_db()
        async with get_db() as db:
            await db.execute(
                "INSERT INTO channels (name, channel_id, min_age_hours) VALUES ('C', 'UC1', 0)"
            )
            await db.commit()
        added, skipped = await queue_svc._scan_channel("UC1", 0, "C")
        assert added == 1
        assert skipped == 0
        async with get_db() as db:
            cursor = await db.execute(
                "SELECT title, max_height FROM queue WHERE video_id = 'abcdefghijk'"
            )
            row = await cursor.fetchone()
        assert row["title"] == "Título em português"
        assert row["max_height"] == 1080

    asyncio.run(run())
