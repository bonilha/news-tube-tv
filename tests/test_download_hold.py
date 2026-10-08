"""The five-video window waits until a scan has been shuffled."""

import asyncio
import time
from contextlib import asynccontextmanager

from app.config import settings
from app.queue import download as dl
from app.queue import service as svc


def _reset_hold() -> None:
    dl._download_hold = 0
    dl._current_download = ""
    dl._dirty = False
    dl._task = None
    dl._abandoned.clear()


def test_schedule_waits_until_the_window_opens(monkeypatch):
    calls = []

    async def sync():
        calls.append("sync")

    monkeypatch.setattr(dl, "sync_downloads", sync)
    _reset_hold()

    async def run():
        dl.hold_downloads()
        dl.hold_downloads()
        dl.schedule_sync()
        await asyncio.sleep(0)
        assert calls == []
        assert dl._task is None
        dl.release_downloads()
        await asyncio.sleep(0)
        assert calls == []
        dl.release_downloads()
        await dl._task

    try:
        asyncio.run(run())
    finally:
        _reset_hold()
    assert calls == ["sync"]


def test_startup_downloads_only_after_the_shuffle(monkeypatch):
    order = []

    async def discard():
        order.append("clean")

    async def scan():
        order.append("scan")
        dl.schedule_sync()
        return {"added": 1}

    async def expire():
        order.append("expire")
        return 0

    async def shuffle(*, reset_files=False):
        order.append("shuffle")
        assert reset_files is False
        return 4

    def schedule():
        order.append("sync" if not dl.downloads_held() else "blocked")

    monkeypatch.setattr(dl, "discard_all_downloads", discard)
    monkeypatch.setattr(svc, "scan_all_channels", scan)
    monkeypatch.setattr(svc, "expire_old_videos", expire)
    monkeypatch.setattr(svc, "shuffle_play_order", shuffle)
    monkeypatch.setattr(dl, "schedule_sync", schedule)
    _reset_hold()

    async def run():
        dl.hold_downloads()
        await svc.startup_broadcast_queue()
        assert dl._download_hold == 0

    try:
        asyncio.run(run())
    finally:
        _reset_hold()
    assert order == ["clean", "scan", "blocked", "expire", "shuffle", "sync"]


def test_clear_downloads_only_after_the_shuffle(monkeypatch):
    order = []

    class Cursor:
        async def fetchall(self):
            return [{"video_id": "abcdefghijk", "local_path": ""}]

    class Db:
        async def execute(self, *_args, **_kwargs):
            order.append("sql")
            return Cursor()

        async def commit(self):
            order.append("commit")

    @asynccontextmanager
    async def get_db():
        yield Db()

    async def discard(_video_id, _local_path=""):
        order.append("files")

    async def scan():
        order.append("scan")
        dl.schedule_sync()
        return {"added": 1}

    async def expire():
        order.append("expire")
        return 0

    async def shuffle(*, reset_files=False):
        order.append("shuffle")
        assert reset_files is False
        return 2

    def schedule():
        order.append("sync" if not dl.downloads_held() else "blocked")

    monkeypatch.setattr(svc, "get_db", get_db)
    monkeypatch.setattr(dl, "discard_video_files", discard)
    monkeypatch.setattr(svc, "scan_all_channels", scan)
    monkeypatch.setattr(svc, "expire_old_videos", expire)
    monkeypatch.setattr(svc, "shuffle_play_order", shuffle)
    monkeypatch.setattr(dl, "schedule_sync", schedule)
    _reset_hold()

    try:
        asyncio.run(svc.clear_queue_and_rescan())
    finally:
        _reset_hold()
    assert order == [
        "sql", "sql", "commit", "files", "scan", "blocked", "expire", "shuffle", "sync",
    ]
    assert dl._download_hold == 0


def test_keep_loop_does_not_start_the_next_id_after_hold(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "VIDEOS_DIR", tmp_path)
    (tmp_path / "downloads").mkdir()
    (tmp_path / "queue").mkdir()
    started = []

    async def one_row(video_id):
        return {
            "video_id": video_id,
            "status": "pending",
            "published_unix": int(time.time()),
            "local_path": "",
            "duration": 30,
            "download_error": "",
        }

    async def run_download(video_id, _budget):
        started.append(video_id)
        dl.hold_downloads()

    async def rows():
        return []

    async def remove(_paths):
        return None

    monkeypatch.setattr(dl, "_one_row", one_row)
    monkeypatch.setattr(dl, "_queue_rows", rows)
    monkeypatch.setattr(dl, "_run_download", run_download)
    monkeypatch.setattr(dl, "raw_download", lambda _video_id: None)
    monkeypatch.setattr(dl, "_enough_free_space", lambda: True)
    monkeypatch.setattr(dl, "remove_files", remove)
    monkeypatch.setattr(dl, "_kill_media_tools", lambda: None)
    _reset_hold()

    try:
        asyncio.run(dl._download_keep(["aaaaaaaaaaa", "bbbbbbbbbbb"], dl.play_order_generation()))
    finally:
        _reset_hold()
    assert started == ["aaaaaaaaaaa"]
