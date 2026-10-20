"""Half the ready buffer is the deadline for the video that is not on disk yet."""

import asyncio
import subprocess
import time
from pathlib import Path

from app.config import settings
from app.queue import download as dl


def test_deadline_is_half_the_ready_videos_that_are_not_on_air():
    rows = [
        {"video_id": "on", "duration": 9999, "local_path": "x"},
        {"video_id": "a", "duration": 10 * 60, "local_path": "x"},
        {"video_id": "b", "duration": 20 * 60, "local_path": "x"},
        {"video_id": "c", "duration": 12 * 60, "local_path": "x"},
        {"video_id": "missing", "duration": 30 * 60, "local_path": ""},
    ]
    ready = lambda row: bool(row.get("local_path"))
    assert dl.ready_wait_seconds(rows, "on", ready) == 21 * 60


def test_deadline_is_zero_when_nothing_else_is_ready():
    rows = [{"video_id": "on", "duration": 600, "local_path": "x"}]
    assert dl.ready_wait_seconds(rows, "on", lambda row: True) == 0
    assert dl.ready_wait_seconds([], "", lambda row: True) == 0


def test_a_missed_deadline_moves_on_to_the_next_id(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "VIDEOS_DIR", tmp_path)
    (tmp_path / "downloads").mkdir()
    (tmp_path / "queue").mkdir()
    monkeypatch.setattr(dl, "_enough_free_space", lambda: True)
    monkeypatch.setattr(dl, "_kill_media_tools", lambda: None)
    monkeypatch.setattr(dl, "ready_wait_seconds", lambda rows, on_air_id="", is_ready=None: 12)

    seen = []

    async def one_row(video_id):
        return {
            "video_id": video_id,
            "status": "pending",
            "published_unix": int(time.time()),
            "duration": 60,
            "download_error": "",
            "local_path": "",
        }

    async def store_error(video_id, message):
        seen.append(("error", video_id, message))

    async def run_download(video_id, budget, _normalize=True, *args, **kwargs):
        seen.append(("download", video_id, budget))
        if video_id == "late":
            raise TimeoutError

    monkeypatch.setattr(dl, "_one_row", one_row)
    monkeypatch.setattr(dl, "_queue_rows", lambda: _empty_rows())
    monkeypatch.setattr(dl, "_store_error", store_error)
    monkeypatch.setattr(dl, "_store_path", lambda *args: _noop())
    monkeypatch.setattr(dl, "remove_files", lambda paths: _noop())
    monkeypatch.setattr(dl, "_run_download", run_download)

    asyncio.run(dl._download_keep(["late", "next"], dl.play_order_generation()))

    assert ("download", "late", 12) in seen
    assert ("error", "late", dl._LATE) in seen
    assert ("download", "next", 12) in seen


def test_loudnorm_after_a_finished_download_is_not_the_deadline(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "VIDEOS_DIR", tmp_path)
    (tmp_path / "downloads").mkdir()
    (tmp_path / "queue").mkdir()
    monkeypatch.setattr(dl, "_enough_free_space", lambda: True)
    monkeypatch.setattr(dl, "ready_wait_seconds", lambda rows, on_air_id="", is_ready=None: 0.05)

    seen = []

    async def one_row(video_id):
        return {
            "video_id": video_id,
            "status": "pending",
            "published_unix": int(time.time()),
            "duration": 60,
            "download_error": "",
            "local_path": "",
        }

    async def store_error(video_id, message):
        seen.append(("error", video_id, message))

    async def store_path(video_id, path):
        seen.append(("path", video_id))

    def download_mp4(video_id, label=""):
        (dl.downloads_dir() / f"{video_id}.mp4").write_bytes(b"raw")

    def place(video_id, normalize=False, label=""):
        time.sleep(0.2)
        raw = dl.downloads_dir() / f"{video_id}.mp4"
        raw.replace(dl.queue_file(video_id))

    monkeypatch.setattr(dl, "_one_row", one_row)
    monkeypatch.setattr(dl, "_queue_rows", lambda: _empty_rows())
    monkeypatch.setattr(dl, "_store_error", store_error)
    monkeypatch.setattr(dl, "_store_path", store_path)
    monkeypatch.setattr(dl, "remove_files", lambda paths: _noop())
    monkeypatch.setattr(dl, "_download_mp4", download_mp4)
    monkeypatch.setattr(dl, "_place_in_queue", place)

    asyncio.run(dl._download_keep(["okvideo111a"], dl.play_order_generation()))

    assert seen == [("path", "okvideo111a")]
    assert (tmp_path / "queue" / "okvideo111a.mp4").is_file()
    assert not (tmp_path / "downloads" / "okvideo111a.mp4").exists()


def test_a_slow_yt_dlp_is_still_late_and_the_next_id_runs(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "VIDEOS_DIR", tmp_path)
    (tmp_path / "downloads").mkdir()
    (tmp_path / "queue").mkdir()
    monkeypatch.setattr(dl, "_enough_free_space", lambda: True)
    monkeypatch.setattr(dl, "_kill_media_tools", lambda: None)
    monkeypatch.setattr(dl, "ready_wait_seconds", lambda rows, on_air_id="", is_ready=None: 0.05)

    seen = []
    placed = []

    async def one_row(video_id):
        return {
            "video_id": video_id,
            "status": "pending",
            "published_unix": int(time.time()),
            "duration": 60,
            "download_error": "",
            "local_path": "",
        }

    async def store_error(video_id, message):
        seen.append(("error", video_id, message))

    async def store_path(video_id, path):
        seen.append(("path", video_id))

    def download_mp4(video_id, label=""):
        if video_id == "latevideo11":
            time.sleep(0.3)
        (dl.downloads_dir() / f"{video_id}.mp4").write_bytes(b"raw")

    def place(video_id, normalize=False, label=""):
        placed.append(video_id)
        (dl.downloads_dir() / f"{video_id}.mp4").replace(dl.queue_file(video_id))

    monkeypatch.setattr(dl, "_one_row", one_row)
    monkeypatch.setattr(dl, "_queue_rows", lambda: _empty_rows())
    monkeypatch.setattr(dl, "_store_error", store_error)
    monkeypatch.setattr(dl, "_store_path", store_path)
    monkeypatch.setattr(dl, "remove_files", lambda paths: _noop())
    monkeypatch.setattr(dl, "_download_mp4", download_mp4)
    monkeypatch.setattr(dl, "_place_in_queue", place)

    asyncio.run(dl._download_keep(
        ["latevideo11", "nextvideo11"], dl.play_order_generation(),
    ))

    assert ("error", "latevideo11", dl._LATE) in seen
    assert ("path", "nextvideo11") in seen
    assert placed == ["nextvideo11"]


def test_probe_failure_is_one_short_call(monkeypatch):
    seen = {}

    def boom(cmd, timeout):
        seen["cmd"] = cmd
        seen["timeout"] = timeout
        seen["calls"] = seen.get("calls", 0) + 1
        raise subprocess.TimeoutExpired(cmd, timeout)

    monkeypatch.setattr(dl, "_run_captured", boom)
    assert dl._probe_media(Path("HnSswBxQCfw.mp4")) == ("", "", 0, 0)
    assert seen["calls"] == 1
    assert seen["timeout"] == dl._PROBE_TIMEOUT_SEC == 8
    assert seen["cmd"][0] == "ffprobe"
    assert "-probesize" in seen["cmd"]
    assert "-analyzeduration" in seen["cmd"]


async def _empty_rows():
    return []


async def _noop():
    return None
