"""A video with no MP4 must not park the broadcast in the bumper forever.

Rule: while waiting, if the queue is down to three or fewer ready files and the
pass already aired something, skip the missing one. At startup, and while the
buffer is healthy, keep waiting for the download.
"""

import asyncio
import itertools

import pytest

from app import cycle as cycle_mod
from app.cycle import CycleManager, SCENE_BUMPER, SCENE_PROGRAM

_PLAYING = {"mediaState": "OBS_MEDIA_STATE_PLAYING", "mediaCursor": 1000, "mediaDuration": 5000}
_ENDED = {"mediaState": "OBS_MEDIA_STATE_ENDED", "mediaCursor": 5000, "mediaDuration": 5000}
_NONE = {"mediaState": "OBS_MEDIA_STATE_NONE", "mediaCursor": 0, "mediaDuration": 0}


class FakeObs:
    """Media that starts and ends on every poll, so the bumper always finishes."""

    def __init__(self, statuses):
        self._statuses = itertools.cycle(statuses)
        self.switched = []

    async def get_media_input_status(self, input_name):
        return next(self._statuses)

    async def trigger_media(self, input_name, action):
        return True

    async def set_input_settings(self, input_name, new_settings):
        return True

    async def switch_scene(self, scene_name):
        self.switched.append(scene_name)


class FakeOverlay:
    async def hide(self):
        pass

    async def show_video(self, video_id):
        pass


class FakeDownload:
    def __init__(self):
        self.retries = 0

    def hold_player_release(self, value):
        pass

    def protect_video_ids(self, ids):
        pass

    def note_on_air(self, video_id):
        pass

    def skip_keep_ids(self, ids):
        pass

    def schedule_sync(self):
        pass

    def selected_encoder(self):
        return "libx264"

    def download_blocked(self, row):
        return False

    async def retry_error_videos(self):
        self.retries += 1
        return 0


class FakeQueue:
    def __init__(self, rows=()):
        self.rows = list(rows)

    async def get_queue_full(self):
        return list(self.rows)

    async def mark_status_by_video_id(self, video_id, status):
        pass

    async def rotate_video_to_end(self, video_id):
        pass

    async def maintain_queue(self):
        return {}


class FakeCookies:
    def status(self):
        return {"ok": True, "message": "", "checked_at": 1}


def _row(video_id: str, order: int, ready: bool) -> dict:
    return {
        "id": order,
        "video_id": video_id,
        "title": video_id,
        "status": "pending",
        "play_order": order,
        "published_unix": 1,
        "duration": 60,
        "download_error": "",
        "local_path": f"ready:{video_id}" if ready else "",
    }


def _ready(row) -> bool:
    """Stand in for the file check: a path set means the MP4 is on disk."""
    return bool(row.get("local_path"))


@pytest.fixture
def mgr(monkeypatch):
    manager = CycleManager()
    manager._enabled = True
    manager._bumper_input = "Bumper"
    manager._wait_input = ""
    manager._aired = set()
    manager._pass_items = []
    manager._current_id = ""
    manager._ready_count = 0
    manager._retry_all_errors = False
    manager._message = ""
    manager._error = False
    manager._media_ended = asyncio.Event()
    monkeypatch.setattr(cycle_mod, "overlay", FakeOverlay())
    monkeypatch.setattr(cycle_mod, "queue_download", FakeDownload())
    monkeypatch.setattr(cycle_mod, "yt_cookies", FakeCookies())
    monkeypatch.setattr(cycle_mod, "_playable", _ready)
    monkeypatch.setattr(cycle_mod, "_POLL_SEC", 0)
    monkeypatch.setattr(cycle_mod, "_NO_MEDIA_GRACE", 0)
    monkeypatch.setattr(cycle_mod, "_RETRY_WAIT_SEC", 0)
    monkeypatch.setattr(cycle_mod, "_OBS_BACKOFF_SEC", 0)
    return manager


def run_cycle(mgr, monkeypatch, rows, statuses, stop_after, seconds=5.0):
    """Drive the cycle until OBS switched `stop_after` times, or give up."""
    monkeypatch.setattr(cycle_mod, "queue_svc", FakeQueue(rows))
    obs = FakeObs(statuses)

    async def switch_scene(scene_name):
        obs.switched.append(scene_name)
        if len(obs.switched) >= stop_after:
            mgr._enabled = False

    monkeypatch.setattr(obs, "switch_scene", switch_scene)
    monkeypatch.setattr(cycle_mod, "obs_manager", obs)

    async def _drive():
        task = asyncio.ensure_future(mgr._run_cycle())
        try:
            await asyncio.wait_for(task, timeout=seconds)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            mgr._enabled = False
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass
        return obs

    obs = asyncio.run(_drive())
    return obs


def test_thin_buffer_skips_the_video_without_a_file(mgr, monkeypatch):
    # 'first' airs, so the pass has begun. Then the head has no file and only
    # one video is left ready: skip it and keep broadcasting.
    rows = [_row("first", 1, True), _row("broken", 2, False), _row("second", 3, True)]
    obs = run_cycle(mgr, monkeypatch, rows, [_PLAYING, _ENDED], stop_after=6)
    assert "broken" in mgr._aired
    assert obs.switched.count(SCENE_PROGRAM) >= 2


def test_cold_start_waits_instead_of_burning_the_pass(mgr, monkeypatch):
    # Nothing has aired yet: the missing head is kept, not marked as transmitido.
    rows = [_row("broken", 1, False), _row("a", 2, True), _row("b", 3, True)]
    run_cycle(mgr, monkeypatch, rows, [_PLAYING, _ENDED], stop_after=6)
    assert "broken" not in mgr._aired


def test_healthy_buffer_waits_for_the_download(mgr, monkeypatch):
    # Four ready files are behind the head: above the floor of three, so
    # waiting is cheap and the download gets its chance.
    rows = [
        _row("first", 1, True),
        _row("broken", 2, False),
        _row("a", 3, True),
        _row("b", 4, True),
        _row("c", 5, True),
        _row("d", 6, True),
    ]
    run_cycle(mgr, monkeypatch, rows, [_PLAYING, _ENDED], stop_after=6)
    assert "broken" not in mgr._aired
    assert "Aguardando" in mgr._message


def test_no_ready_file_says_why_nothing_is_downloading(mgr, monkeypatch):
    rows = [_row("broken", 1, False), _row("also", 2, False)]
    run_cycle(mgr, monkeypatch, rows, [_PLAYING, _ENDED], stop_after=4)
    assert "broken" not in mgr._aired
    assert mgr._message.startswith("Aguardando")


def test_no_ready_file_names_the_cookie_problem(mgr, monkeypatch):
    class DeadCookies(FakeCookies):
        def status(self):
            return {"ok": False, "message": "Cookies recusados.", "checked_at": 1}

    monkeypatch.setattr(cycle_mod, "yt_cookies", DeadCookies())
    rows = [_row("broken", 1, False)]
    run_cycle(mgr, monkeypatch, rows, [_PLAYING, _ENDED], stop_after=4)
    assert "Cookies recusados." in mgr._message


def test_wrapping_the_pass_retries_failed_downloads(mgr, monkeypatch):
    # Every video aired: the lap restarts, and that is the retry point.
    rows = [_row("a", 1, True), _row("b", 2, True)]
    run_cycle(mgr, monkeypatch, rows, [_PLAYING, _ENDED], stop_after=6)
    assert cycle_mod.queue_download.retries >= 1
