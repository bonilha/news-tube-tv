"""The bumper wait must have an exit. Regression for the overnight stuck SCENE_BUMPER."""

import asyncio

import pytest

from app import cycle as cycle_mod
from app.cycle import CycleManager


class FakeObs:
    def __init__(self, statuses):
        self.statuses = list(statuses)
        self.switched = []
        self.actions = []
        self.is_streaming = False

    async def get_media_input_status(self, input_name):
        if len(self.statuses) > 1:
            return self.statuses.pop(0)
        return self.statuses[0]

    async def trigger_media(self, input_name, action):
        self.actions.append((input_name, action))
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

    async def ensure_overlay(self):
        pass


class FakeDownload:
    def __init__(self):
        self.holds = []

    def hold_player_release(self, value):
        self.holds.append(value)

    def protect_video_ids(self, ids):
        pass

    def note_on_air(self, video_id):
        pass

    def skip_keep_ids(self, ids):
        pass

    def schedule_sync(self):
        pass

    def download_blocked(self, row):
        return False

    def channel_disabled(self, row):
        from app.queue.download import channel_disabled
        return channel_disabled(row)

    def selected_encoder(self):
        return "libx264"

    async def retry_error_videos(self):
        return 0


class FakeQueue:
    def __init__(self, rows=()):
        self.rows = list(rows)

    async def get_queue_full(self):
        return list(self.rows)

    async def mark_status_by_video_id(self, queue_id, status):
        pass

    async def rotate_video_to_end(self, video_id):
        pass

    async def maintain_queue(self):
        return {}


@pytest.fixture
def mgr(monkeypatch):
    manager = CycleManager()
    manager._enabled = True
    manager._bumper_input = "Bumper"
    manager._wait_input = ""
    manager._aired = set()
    manager._pass_items = []
    manager._current_id = ""
    manager._media_ended = asyncio.Event()
    monkeypatch.setattr(cycle_mod, "overlay", FakeOverlay())
    monkeypatch.setattr(cycle_mod, "queue_download", FakeDownload())
    monkeypatch.setattr(cycle_mod, "queue_svc", FakeQueue())
    monkeypatch.setattr(cycle_mod, "_POLL_SEC", 0)
    monkeypatch.setattr(cycle_mod, "_NO_MEDIA_GRACE", 0)
    monkeypatch.setattr(cycle_mod, "_RETRY_WAIT_SEC", 0)
    monkeypatch.setattr(cycle_mod, "_OBS_BACKOFF_SEC", 0)
    return manager


def patch_obs(mgr, monkeypatch, statuses):
    obs = FakeObs(statuses)
    monkeypatch.setattr(cycle_mod, "obs_manager", obs)
    return obs


def test_never_starting_media_does_not_wait_forever(mgr, monkeypatch):
    none = {"mediaState": "OBS_MEDIA_STATE_NONE", "mediaCursor": 0, "mediaDuration": 0}
    patch_obs(mgr, monkeypatch, [none])
    assert asyncio.run(mgr._wait_end("Bumper")) is False
    assert mgr._error is True


def test_silent_obs_does_not_wait_forever(mgr, monkeypatch):
    patch_obs(mgr, monkeypatch, [{}])
    assert asyncio.run(mgr._wait_end("Bumper")) is False


def test_ended_without_duration_is_a_missing_file(mgr, monkeypatch):
    # A deleted file reports ENDED with no duration. That is not a clip that finished.
    stopped = {"mediaState": "OBS_MEDIA_STATE_ENDED", "mediaCursor": 0, "mediaDuration": 0}
    patch_obs(mgr, monkeypatch, [stopped])
    assert asyncio.run(mgr._wait_end("Bumper")) is False


def test_playing_media_that_ends_returns_true(mgr, monkeypatch):
    obs = patch_obs(mgr, monkeypatch, [
        {"mediaState": "OBS_MEDIA_STATE_PLAYING", "mediaCursor": 1000, "mediaDuration": 5000},
        {"mediaState": "OBS_MEDIA_STATE_ENDED", "mediaCursor": 5000, "mediaDuration": 5000},
    ])
    assert asyncio.run(mgr._wait_end("Bumper")) is True
    assert obs.actions and obs.actions[0][1] == cycle_mod._MEDIA_RESTART


def test_paused_media_gives_up(mgr, monkeypatch):
    monkeypatch.setattr(cycle_mod, "_NO_MEDIA_GRACE", 3600)
    monkeypatch.setattr(cycle_mod, "_NO_PROGRESS_SEC", -1)
    patch_obs(mgr, monkeypatch, [
        {"mediaState": "OBS_MEDIA_STATE_PLAYING", "mediaCursor": 100, "mediaDuration": 5000},
        {"mediaState": "OBS_MEDIA_STATE_PAUSED", "mediaCursor": 100, "mediaDuration": 5000},
    ])
    assert asyncio.run(mgr._wait_end("Bumper")) is False


def test_cycle_rearms_bumper_and_stops_when_disabled(mgr, monkeypatch):
    none = {"mediaState": "OBS_MEDIA_STATE_NONE", "mediaCursor": 0, "mediaDuration": 0}
    obs = patch_obs(mgr, monkeypatch, [none])

    # Overnight bug: the bumper never ends and the cycle parks in SCENE_BUMPER.
    # Now the scene is re-armed and turning the player off is enough to leave.
    original = obs.switch_scene

    async def switch_scene(scene_name):
        await original(scene_name)
        if len(obs.switched) >= 3:
            mgr._enabled = False

    monkeypatch.setattr(obs, "switch_scene", switch_scene)
    asyncio.run(mgr._run_cycle())
    assert len(obs.switched) >= 3
    assert mgr.state == "stopped"
