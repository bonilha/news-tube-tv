"""Schedule math and the graceful end of a transmission."""

import asyncio
import time
from contextlib import asynccontextmanager
from datetime import datetime

import pytest

from app import broadcast as broadcast_mod
from app import cycle as cycle_mod
from app.broadcast import (
    current_limit_seconds,
    decide,
    save_limit_seconds,
    validate_schedule,
)
from app.cycle import SCENE_BUMPER, SCENE_PROGRAM, CycleManager
from app.queue.download import buffer_ids
from tests.test_cycle_skip import FakeCookies, FakeDownload, FakeObs, FakeOverlay, FakeQueue, _row


def _row_sched(**extra):
    base = {
        "schedule_mode": "daily",
        "schedule_time": "08:00",
        "schedule_date": "",
        "last_fired_on": "",
        "armed_for": "",
    }
    base.update(extra)
    return base


def test_schedule_off_keeps_downloads_open_without_starting():
    now = datetime(2026, 10, 4, 15, 0)
    decision = decide(
        now,
        _row_sched(schedule_mode="off", schedule_time="", schedule_date=""),
        cycle_on=False,
        streaming=False,
        ready=False,
    )
    assert decision["open_downloads"] is True
    assert decision["start"] is False


def test_fifteen_minutes_before_opens_downloads_only():
    now = datetime(2026, 10, 3, 7, 45)
    decision = decide(now, _row_sched(), cycle_on=False, streaming=False, ready=False)
    assert decision["open_downloads"] is True
    assert decision["start"] is False
    assert decision["armed_for"] == "2026-10-03T08:00"


def test_on_the_hour_starts_once():
    now = datetime(2026, 10, 3, 8, 0)
    decision = decide(
        now,
        _row_sched(armed_for="2026-10-03T08:00"),
        cycle_on=False,
        streaming=False,
        ready=True,
    )
    assert decision["start"] is True
    assert decision["last_fired_on"] == "2026-10-03"


def test_same_day_does_not_start_again():
    now = datetime(2026, 10, 3, 8, 5)
    decision = decide(
        now,
        _row_sched(last_fired_on="2026-10-03"),
        cycle_on=False,
        streaming=False,
        ready=True,
    )
    assert decision["start"] is False
    assert decision["open_downloads"] is False


def test_more_than_fifteen_minutes_late_does_not_start():
    now = datetime(2026, 10, 3, 8, 16)
    decision = decide(now, _row_sched(), cycle_on=False, streaming=False, ready=True)
    assert decision["start"] is False
    assert decision["open_downloads"] is False


def test_armed_wait_starts_when_the_file_appears():
    now = datetime(2026, 10, 3, 8, 40)
    waiting = decide(
        now,
        _row_sched(armed_for="2026-10-03T08:00"),
        cycle_on=False,
        streaming=False,
        ready=False,
    )
    assert waiting["open_downloads"] is True
    assert waiting["start"] is False
    ready = decide(
        now,
        _row_sched(armed_for="2026-10-03T08:00"),
        cycle_on=False,
        streaming=False,
        ready=True,
    )
    assert ready["start"] is True


def test_once_turns_off_after_it_fires():
    now = datetime(2026, 10, 3, 8, 0)
    row = _row_sched(
        schedule_mode="once",
        schedule_date="2026-10-03",
        armed_for="2026-10-03T08:00",
    )
    decision = decide(now, row, cycle_on=False, streaming=False, ready=True)
    assert decision["start"] is True
    assert decision["schedule_mode"] == "off"


def test_once_in_the_past_without_arm_turns_off():
    now = datetime(2026, 10, 3, 9, 0)
    row = _row_sched(schedule_mode="once", schedule_date="2026-10-03", schedule_time="08:00")
    decision = decide(now, row, cycle_on=False, streaming=False, ready=True)
    assert decision["start"] is False
    assert decision["schedule_mode"] == "off"


def test_schedule_form_rejects_a_past_date():
    with pytest.raises(ValueError):
        validate_schedule("once", "08:00", "2020-01-01", datetime(2026, 10, 3, 12, 0))


def _limit_db(monkeypatch, saved):
    class Db:
        async def execute(self, _sql, params=()):
            saved.append(params)

        async def commit(self):
            pass

    @asynccontextmanager
    async def get_db():
        yield Db()

    monkeypatch.setattr(broadcast_mod, "get_db", get_db)


def test_continuous_zero_and_the_grid_bounds_are_saved(monkeypatch):
    saved = []
    _limit_db(monkeypatch, saved)
    asyncio.run(save_limit_seconds(0))
    asyncio.run(save_limit_seconds(1800))
    asyncio.run(save_limit_seconds(86400))
    assert saved == [(0,), (1800,), (86400,)]


def test_values_outside_the_grid_are_refused(monkeypatch):
    _limit_db(monkeypatch, [])
    for seconds in (1799, 86460, -1800):
        with pytest.raises(ValueError):
            asyncio.run(save_limit_seconds(seconds))


def test_a_zero_limit_reads_back_as_continuous(monkeypatch):
    async def control():
        return {"limit_seconds": 0}

    monkeypatch.setattr(broadcast_mod, "get_control", control)
    assert asyncio.run(current_limit_seconds()) == 0


def test_disabled_channel_is_outside_the_five():
    rows = [
        {"id": 1, "video_id": "off", "status": "pending", "published_unix": int(time.time()),
         "play_order": 1, "duration": 60, "download_error": "", "active": 0},
        {"id": 2, "video_id": "on", "status": "pending", "published_unix": int(time.time()),
         "play_order": 2, "duration": 60, "download_error": "", "active": 1},
    ]
    assert buffer_ids(rows, 5, set()) == ["on"]


def _drive(mgr, monkeypatch, rows, obs):
    monkeypatch.setattr(cycle_mod, "queue_svc", FakeQueue(rows))
    monkeypatch.setattr(cycle_mod, "obs_manager", obs)
    monkeypatch.setattr(cycle_mod, "overlay", FakeOverlay())
    monkeypatch.setattr(cycle_mod, "queue_download", FakeDownload())
    monkeypatch.setattr(cycle_mod, "yt_cookies", FakeCookies())
    monkeypatch.setattr(cycle_mod, "_playable", lambda row: bool(row.get("local_path")))
    monkeypatch.setattr(cycle_mod, "_POLL_SEC", 0)
    monkeypatch.setattr(cycle_mod, "_NO_MEDIA_GRACE", 0)
    monkeypatch.setattr(cycle_mod, "_RETRY_WAIT_SEC", 0)
    monkeypatch.setattr(cycle_mod, "_OBS_BACKOFF_SEC", 0)

    async def _run():
        task = asyncio.ensure_future(mgr._run_cycle())
        try:
            await asyncio.wait_for(task, timeout=5)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            mgr._enabled = False
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass
        return obs

    return asyncio.run(_run())


def _manager():
    manager = CycleManager()
    manager._enabled = True
    manager._bumper_input = "Bumper"
    manager._aired = set()
    manager._pass_items = []
    manager._current_id = ""
    manager._ready_count = 0
    manager._retry_all_errors = False
    manager._wait_budget = 0.0
    manager._unready_since = {}
    manager._message = ""
    manager._error = False
    manager._finish_requested = False
    manager._on_air_seconds = 0.0
    manager._limit_seconds = 0
    manager._limit_loader = None
    manager._last_media_ms = 0
    manager._media_ended = asyncio.Event()
    return manager


def test_finish_waits_for_the_video_then_plays_the_bumper(monkeypatch):
    manager = _manager()
    rows = [_row("first", 1, True), _row("second", 2, True)]
    obs = FakeObs([
        {"mediaState": "OBS_MEDIA_STATE_PLAYING", "mediaCursor": 100, "mediaDuration": 5000},
        {"mediaState": "OBS_MEDIA_STATE_ENDED", "mediaCursor": 5000, "mediaDuration": 5000},
    ])
    obs.is_streaming = True
    original = obs.switch_scene

    async def switch_scene(name):
        await original(name)
        if name == SCENE_PROGRAM:
            manager.request_finish()

    obs.switch_scene = switch_scene
    holds = {"n": 0}

    def _hold():
        holds["n"] += 1

    monkeypatch.setattr("app.broadcast.hold_until_needed", _hold)
    _drive(manager, monkeypatch, rows, obs)
    assert obs.switched == [SCENE_BUMPER, SCENE_PROGRAM, SCENE_BUMPER]
    assert obs.stopped is True
    assert manager.is_enabled is False
    assert holds["n"] == 1


def test_limit_after_a_video_still_plays_one_bumper(monkeypatch):
    manager = _manager()
    manager._limit_seconds = 3

    class TimedObs(FakeObs):
        async def get_media_input_status(self, input_name):
            state = next(self._statuses)
            data = dict(state)
            if input_name == "Bumper":
                data["mediaDuration"] = 1000
                data["mediaCursor"] = 1000
            else:
                data["mediaDuration"] = 5000
                data["mediaCursor"] = 5000
            return data

    obs = TimedObs([
        {"mediaState": "OBS_MEDIA_STATE_PLAYING", "mediaCursor": 100, "mediaDuration": 5000},
    ])
    obs.is_streaming = True
    monkeypatch.setattr("app.broadcast.hold_until_needed", lambda: None)
    _drive(manager, monkeypatch, [_row("first", 1, True), _row("second", 2, True)], obs)
    assert SCENE_PROGRAM in obs.switched
    assert obs.switched[-1] == SCENE_BUMPER
    assert obs.switched.count(SCENE_PROGRAM) == 1
    assert manager.is_enabled is False


def test_simulation_does_not_accumulate(monkeypatch):
    manager = _manager()
    manager._limit_seconds = 1
    obs = FakeObs([
        {"mediaState": "OBS_MEDIA_STATE_PLAYING", "mediaCursor": 100, "mediaDuration": 5000},
        {"mediaState": "OBS_MEDIA_STATE_ENDED", "mediaCursor": 5000, "mediaDuration": 5000},
    ])
    obs.is_streaming = False

    async def switch_scene(name):
        obs.switched.append(name)
        if obs.switched.count(SCENE_PROGRAM) >= 2:
            manager._enabled = False

    obs.switch_scene = switch_scene
    _drive(manager, monkeypatch, [_row("first", 1, True), _row("second", 2, True)], obs)
    assert manager._on_air_seconds == 0
    assert obs.stopped is False
