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

    detail_published = int(time.time()) - 7200

    async def detail(_video_id, _fallback):
        assert open_depth == 0
        return "Título em português", 1080, False, detail_published, 90

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
        added, skipped, _reasons = await queue_svc._scan_channel("UC1", 0, "C")
        assert added == 1
        assert skipped == 0
        async with get_db() as db:
            cursor = await db.execute(
                "SELECT title, max_height, published_unix FROM queue WHERE video_id = 'abcdefghijk'"
            )
            row = await cursor.fetchone()
        assert row["title"] == "Título em português"
        assert row["max_height"] == 1080
        assert row["published_unix"] == detail_published

    asyncio.run(run())


def test_detail_publish_time_drops_a_video_the_list_called_new(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "DB_PATH", tmp_path / "scan.db")
    now = int(time.time())

    async def videos(_channel_id, sort_by="newest"):
        return [{
            "videoId": "vZvLAxxNbk1",
            "title": "English list title",
            "author": "Canal",
            "lengthSeconds": 600,
            "published": now,
            "type": "video",
            "videoThumbnails": [],
        }]

    async def no_shorts(_channel_id):
        return set()

    async def detail(_video_id, _fallback):
        return "LULA E VELHA MÍDIA", 1080, False, now - (120 * 86400), 600

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
            cursor = await db.execute("SELECT id FROM channels WHERE channel_id = 'UC1'")
            channel_id = (await cursor.fetchone())["id"]
            await db.execute(
                """INSERT INTO queue
                   (channel_id, video_id, title, author, duration, published_unix, status)
                   VALUES (?, 'vZvLAxxNbk1', 'stale', 'Canal', 600, ?, 'pending')""",
                (channel_id, now),
            )
            await db.commit()
        added, skipped, _reasons = await queue_svc._scan_channel("UC1", 0, "C")
        assert added == 0
        assert skipped == 1
        async with get_db() as db:
            cursor = await db.execute(
                "SELECT video_id FROM queue WHERE video_id = 'vZvLAxxNbk1'"
            )
            row = await cursor.fetchone()
        assert row is None

    asyncio.run(run())


def test_shorts_shelf_published_zero_uses_the_video_time(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "DB_PATH", tmp_path / "scan.db")
    now = int(time.time())

    async def videos(_channel_id, sort_by="newest"):
        return []

    async def shorts(_channel_id, sort_by="newest"):
        return [{
            "videoId": "J4j65eB51Uc",
            "title": "English shelf title",
            "author": "Jeffrey Chiquini",
            "lengthSeconds": 60,
            "published": 0,
            "type": "video",
            "videoThumbnails": [],
        }]

    async def detail(_video_id, _fallback):
        return "Estamos na frente", 1080, False, now - 5 * 3600, 104

    monkeypatch.setattr(queue_svc.invidious, "channel_videos", videos)
    monkeypatch.setattr(queue_svc.invidious, "channel_shorts", shorts)
    monkeypatch.setattr(queue_svc.invidious, "video_title", detail)

    async def run():
        await init_db()
        async with get_db() as db:
            await db.execute(
                "INSERT INTO channels (name, channel_id, min_age_hours, enable_shorts) "
                "VALUES ('Jeffrey Chiquini', 'UC1', 2, 1)"
            )
            await db.commit()
        added, skipped, _reasons = await queue_svc._scan_channel("UC1", 2, "Jeffrey Chiquini")
        assert added == 1
        assert skipped == 0
        async with get_db() as db:
            cursor = await db.execute(
                "SELECT is_short, duration, published_unix, title FROM queue "
                "WHERE video_id = 'J4j65eB51Uc'"
            )
            row = await cursor.fetchone()
        assert row["is_short"] == 1
        assert row["duration"] == 104
        assert row["published_unix"] == now - 5 * 3600
        assert row["title"] == "Estamos na frente"

    asyncio.run(run())


def test_shorts_shelf_still_drops_a_detail_older_than_a_day(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "DB_PATH", tmp_path / "scan.db")
    now = int(time.time())

    async def videos(_channel_id, sort_by="newest"):
        return []

    async def shorts(_channel_id, sort_by="newest"):
        return [{
            "videoId": "oldshort1111",
            "title": "Old",
            "author": "Canal",
            "lengthSeconds": 60,
            "published": 0,
            "type": "video",
            "videoThumbnails": [],
        }]

    async def detail(_video_id, _fallback):
        return "Velho", 1080, False, now - 30 * 3600, 40

    monkeypatch.setattr(queue_svc.invidious, "channel_videos", videos)
    monkeypatch.setattr(queue_svc.invidious, "channel_shorts", shorts)
    monkeypatch.setattr(queue_svc.invidious, "video_title", detail)

    async def run():
        await init_db()
        async with get_db() as db:
            await db.execute(
                "INSERT INTO channels (name, channel_id, min_age_hours, enable_shorts) "
                "VALUES ('C', 'UC1', 0, 1)"
            )
            await db.commit()
        added, skipped, _reasons = await queue_svc._scan_channel("UC1", 0, "C")
        assert added == 0
        assert skipped == 1
        async with get_db() as db:
            cursor = await db.execute(
                "SELECT video_id FROM queue WHERE video_id = 'oldshort1111'"
            )
            row = await cursor.fetchone()
        assert row is None

    asyncio.run(run())


def test_relative_text_beats_utc_midnight():
    from app.yt.invidious import resolve_published

    now = 1_792_250_000
    midnight = now - (now % 86400) - 86400
    resolved = resolve_published(midnight, "16 horas atrás", now)
    assert resolved == now - 16 * 3600
    assert resolve_published(midnight, "23 hours ago", now) == now - 23 * 3600
    assert resolve_published(midnight, "1 dia atrás", now) == now - 86400
    assert resolve_published(midnight, "1 day ago", now) == now - 86400
    assert resolve_published(midnight, None, now) is None
    precise = now - 3 * 3600
    assert resolve_published(precise, "16 de out. de 2026", now) == precise
    assert resolve_published(precise, "1 dia atrás", now) == precise


def test_scan_keeps_a_16h_video_when_detail_unix_is_yesterday_midnight(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "DB_PATH", tmp_path / "scan.db")
    now = int(time.time())
    midnight = now - (now % 86400) - 86400

    async def videos(_channel_id, sort_by="newest"):
        return [{
            "videoId": "abcdefghijk",
            "title": "English list title",
            "author": "ANCAPSU",
            "lengthSeconds": 600,
            "published": now - 16 * 3600,
            "type": "video",
            "videoThumbnails": [],
        }]

    async def no_shorts(_channel_id):
        return set()

    class _Resp:
        status_code = 200

        def json(self):
            return {
                "title": "Título em português",
                "published": midnight,
                "publishedText": "16 horas atrás",
                "lengthSeconds": 600,
                "adaptiveFormats": [{"size": "1920x1080"}],
            }

    class _Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def get(self, url, params=None):
            return _Resp()

    monkeypatch.setattr(queue_svc.invidious.httpx, "AsyncClient", _Client)
    monkeypatch.setattr(queue_svc.invidious, "channel_videos", videos)
    monkeypatch.setattr(queue_svc.invidious, "channel_shorts_ids", no_shorts)

    async def run():
        await init_db()
        async with get_db() as db:
            await db.execute(
                "INSERT INTO channels (name, channel_id, min_age_hours) VALUES ('ANCAPSU', 'UC1', 0)"
            )
            await db.commit()
        added, skipped, _reasons = await queue_svc._scan_channel("UC1", 0, "ANCAPSU")
        assert added == 1
        assert skipped == 0
        async with get_db() as db:
            cursor = await db.execute(
                "SELECT published_unix FROM queue WHERE video_id = 'abcdefghijk'"
            )
            row = await cursor.fetchone()
        assert row["published_unix"] != midnight
        assert now - int(row["published_unix"]) < 24 * 3600

    asyncio.run(run())


def test_google_published_at_is_not_replaced_by_one_day_ago(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "DB_PATH", tmp_path / "scan.db")
    exact = int(time.time()) - 16 * 3600

    async def videos(_channel_id, sort_by="newest"):
        return [{
            "videoId": "abcdefghijk",
            "title": "English list title",
            "author": "ANCAPSU",
            "lengthSeconds": 600,
            "published": exact - 86400,
            "type": "video",
            "videoThumbnails": [],
        }]

    async def no_shorts(_channel_id):
        return set()

    async def detail(_video_id, _fallback):
        # Invidious rounded this to a full day. The scan must keep Google's stamp.
        return "Título em português", 1080, False, exact - 86400, 600

    async def google(ids):
        assert ids == ["abcdefghijk"]
        return {"abcdefghijk": exact}

    monkeypatch.setattr(queue_svc.invidious, "channel_videos", videos)
    monkeypatch.setattr(queue_svc.invidious, "channel_shorts_ids", no_shorts)
    monkeypatch.setattr(queue_svc.invidious, "video_title", detail)
    monkeypatch.setattr(queue_svc.google_yt, "fetch_videos_published_at", google)

    async def run():
        await init_db()
        async with get_db() as db:
            await db.execute(
                "INSERT INTO channels (name, channel_id, min_age_hours) VALUES ('ANCAPSU', 'UC1', 0)"
            )
            await db.commit()
        added, skipped, _reasons = await queue_svc._scan_channel("UC1", 0, "ANCAPSU")
        assert added == 1
        assert skipped == 0
        async with get_db() as db:
            cursor = await db.execute(
                "SELECT published_unix FROM queue WHERE video_id = 'abcdefghijk'"
            )
            row = await cursor.fetchone()
        assert row["published_unix"] == exact

    asyncio.run(run())
