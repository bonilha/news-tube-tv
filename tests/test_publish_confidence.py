"""A publish time the Invidious instance only knows as a day must not sink the video.

The incident: the instance answered published 00:00:00 (a whole UTC day) for a
whole channel. `resolve_published` threw that stamp away, and the day-rounded
"1 dia atrás" landed exactly on 24.0h, so the scan dropped — and even deleted —
videos that were only hours old.
"""

import asyncio
import os
import time
from pathlib import Path

import pytest

from app.config import settings
from app.database import get_db, init_db
from app.queue import download as dl
from app.queue import service as queue_svc
from app.yt.invidious import (
    APPROX,
    EXACT,
    UNKNOWN,
    is_eligible,
    publish_confidence,
    resolve_published,
    window_hours,
)


@pytest.fixture
def db_path(request, monkeypatch):
    """A private SQLite file next to the repository.

    pytest's numbered temp root needs a writable parent, which not every Windows
    profile grants. One directory created here is enough for the whole file.
    """
    directory = Path(".test-db")
    os.makedirs(directory, exist_ok=True)
    path = directory / f"{request.node.name[:40].replace('/', '_')}-{os.getpid()}-{id(request)}.db"
    monkeypatch.setattr(settings, "DB_PATH", path)

    def cleanup():
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(str(path) + suffix)
            except OSError:
                pass

    request.addfinalizer(cleanup)
    return path


def _now_midnight() -> int:
    now = int(time.time())
    return now - (now % 86400)


def _yesterday_midnight() -> int:
    return _now_midnight() - 86400


def test_a_date_without_a_time_is_approximate():
    assert publish_confidence(_yesterday_midnight()) == APPROX
    assert publish_confidence(int(time.time()) - 7200) == EXACT
    assert publish_confidence(None) == UNKNOWN
    # A text that only counted whole days is a lower bound too.
    assert publish_confidence(int(time.time()) - 7200, {"publishedText": "1 dia atrás"}) == APPROX


def test_a_midnight_stamp_is_kept_and_judged_wider():
    now = int(time.time())
    unix = resolve_published(_now_midnight(), None, now)
    confidence = publish_confidence(unix)
    assert unix == _now_midnight()
    assert confidence == APPROX
    # Inside the day it names, in either direction.
    eligible, reason = is_eligible(
        {"video_id": "abcdefghijk", "duracao": 600, "published": unix},
        0, now_unix=now, age_confidence=confidence,
    )
    assert eligible is True, reason
    # "1 dia atrás" is a whole day, so 24.0h old is still inside the window.
    eligible, reason = is_eligible(
        {"video_id": "abcdefghijk", "duracao": 600, "published": now - 86400},
        0, now_unix=now, age_confidence=APPROX,
    )
    assert eligible is True, reason
    # Day before that is out.
    eligible, reason = is_eligible(
        {"video_id": "abcdefghijk", "duracao": 600, "published": now - 2 * 86400},
        0, now_unix=now, age_confidence=APPROX,
    )
    assert eligible is False and "24h" in reason
    assert window_hours(APPROX) == 48
    assert window_hours(EXACT) == 24


def test_an_exact_stamp_keeps_the_original_gate():
    now = int(time.time())
    meta = {"video_id": "abcdefghijk", "duracao": 600, "published": now - 3600}
    eligible, reason = is_eligible(meta, 2, now_unix=now, age_confidence=EXACT)
    assert eligible is False and "Novo demais" in reason
    assert resolve_published(_now_midnight(), None, now) == _now_midnight()


def test_scan_queues_a_date_only_video_the_detail_cannot_date(monkeypatch, db_path):
    now = int(time.time())

    async def videos(_channel_id, sort_by="newest"):
        return [{
            "videoId": "abcdefghijk",
            "title": "Título na lista",
            "author": "Canal",
            "lengthSeconds": 600,
            "published": now - 8 * 3600,
            "publishedText": "8 horas atrás",
            "type": "video",
            "videoThumbnails": [],
        }]

    async def no_shorts(_channel_id):
        return set()

    class _Resp:
        status_code = 200

        def json(self):
            # A whole day, no relative text: the instance does not know the hour.
            return {
                "title": "Título em português",
                "published": _now_midnight(),
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

    async def no_google(_ids):
        return {}

    monkeypatch.setattr(queue_svc.google_yt, "fetch_videos_published_at", no_google)

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
                "SELECT published_unix, published_confidence FROM queue WHERE video_id = 'abcdefghijk'"
            )
            row = await cursor.fetchone()
        assert row["published_unix"] == _now_midnight()
        assert row["published_confidence"] == APPROX

    asyncio.run(run())


def test_google_exact_date_survives_a_dead_detail_page(monkeypatch, db_path):
    exact = int(time.time()) - 3 * 3600

    async def videos(_channel_id, sort_by="newest"):
        return [{
            "videoId": "abcdefghijk",
            "title": "Título na lista",
            "author": "Canal",
            "lengthSeconds": 600,
            "published": 0,
            "type": "video",
            "videoThumbnails": [],
        }]

    async def no_shorts(_channel_id):
        return set()

    async def dead_detail(_video_id, _fallback, defer_published=False):
        return "", 0, None, None, 0

    async def google(_ids):
        return {"abcdefghijk": exact}

    monkeypatch.setattr(queue_svc.invidious, "channel_videos", videos)
    monkeypatch.setattr(queue_svc.invidious, "channel_shorts_ids", no_shorts)
    monkeypatch.setattr(queue_svc.invidious, "video_title", dead_detail)
    monkeypatch.setattr(queue_svc.google_yt, "fetch_videos_published_at", google)
    # The list title is the original language, so the scan uses the fallback here.
    monkeypatch.setattr(queue_svc.invidious, "normalize_video", lambda raw: {
        "video_id": raw["videoId"], "title": raw["title"], "author": raw["author"],
        "published": None, "published_confidence": UNKNOWN, "duracao": 600,
        "type": "video", "thumb": "", "paid": False, "is_members": False,
    })

    async def run():
        await init_db()
        async with get_db() as db:
            await db.execute(
                "INSERT INTO channels (name, channel_id, min_age_hours) VALUES ('C', 'UC1', 0)"
            )
            await db.commit()
        added, skipped, _reasons = await queue_svc._scan_channel("UC1", 0, "C")
        assert added == 0, "detail failure must not let an untitled row in"
        assert skipped == 1

    asyncio.run(run())


@pytest.mark.parametrize("detail_confidence", [UNKNOWN, APPROX])
def test_google_exact_date_beats_a_detail_without_one(monkeypatch, db_path, detail_confidence):
    exact = int(time.time()) - 3 * 3600

    async def videos(_channel_id, sort_by="newest"):
        return [{
            "videoId": "abcdefghijk",
            "title": "Título na lista",
            "author": "Canal",
            "lengthSeconds": 600,
            "published": 0,
            "type": "video",
            "videoThumbnails": [],
        }]

    async def no_shorts(_channel_id):
        return set()

    async def detail(_video_id, _fallback, defer_published=False):
        # The endpoint answered, but its date is a whole day (or nothing at all).
        return "Título em português", 1080, False, _now_midnight() if detail_confidence == APPROX else None, 600

    async def google(_ids):
        return {"abcdefghijk": exact}

    monkeypatch.setattr(queue_svc.invidious, "channel_videos", videos)
    monkeypatch.setattr(queue_svc.invidious, "channel_shorts_ids", no_shorts)
    monkeypatch.setattr(queue_svc.invidious, "video_title", detail)
    monkeypatch.setattr(queue_svc.google_yt, "fetch_videos_published_at", google)

    async def run():
        await init_db()
        async with get_db() as db:
            await db.execute(
                "INSERT INTO channels (name, channel_id, min_age_hours) VALUES ('C', 'UC1', 0)"
            )
            await db.commit()
        added, skipped, _reasons = await queue_svc._scan_channel("UC1", 0, "C")
        assert added == 1, f"skipped={skipped}"
        async with get_db() as db:
            cursor = await db.execute(
                "SELECT published_unix, published_confidence, title FROM queue WHERE video_id = 'abcdefghijk'"
            )
            row = await cursor.fetchone()
        assert row["published_unix"] == exact
        assert row["published_confidence"] == EXACT
        assert row["title"] == "Título em português"

    asyncio.run(run())


@pytest.mark.parametrize("now_offset", [0, 3600])
def test_purge_keeps_a_date_only_row_inside_its_window(monkeypatch, db_path, now_offset):
    now = int(time.time()) + now_offset

    async def run():
        await init_db()
        async with get_db() as db:
            await db.execute(
                "INSERT INTO channels (id, name, channel_id) VALUES (1, 'C', 'UC1')"
            )
            await db.execute(
                "INSERT INTO queue (channel_id, video_id, title, author, duration,"
                " published_unix, status, published_confidence)"
                " VALUES (1, 'approxrow01', 't', 'a', 600, ?, 'pending', 'approx')",
                (_now_midnight(),),
            )
            await db.execute(
                "INSERT INTO queue (channel_id, video_id, title, author, duration,"
                " published_unix, status, published_confidence)"
                " VALUES (1, 'stalerow001', 't', 'a', 600, ?, 'pending', 'exact')",
                (now - 30 * 3600,),
            )
            await db.commit()
        monkeypatch.setattr(dl.time, "time", lambda: now)
        removed = await dl.purge_expired()
        assert removed == 1
        async with get_db() as db:
            cursor = await db.execute("SELECT video_id FROM queue")
            left = {row["video_id"] for row in await cursor.fetchall()}
        assert left == {"approxrow01"}
        rows = await dl._queue_rows()
        assert dl.download_blocked(rows[0]) is False

    asyncio.run(run())