"""OBS request failures stay visible and do not pretend the scene changed."""

import asyncio
import logging

from app.obs.manager import OBSManager, SCENE_OFFLINE, SCENE_PROGRAM


def test_request_timeout_names_the_error(monkeypatch, caplog):
    monkeypatch.setattr("app.obs.manager._REQUEST_TIMEOUT_SEC", 0.05)
    manager = OBSManager()
    previous_ws = manager._ws
    previous_scene = manager._current_scene

    class FakeSocket:
        def __init__(self):
            self.closed = False

        async def send(self, _data):
            return None

        async def close(self):
            self.closed = True

    socket = FakeSocket()
    manager._ws = socket
    try:
        with caplog.at_level(logging.ERROR):
            result = asyncio.run(manager._request(
                "SetCurrentProgramScene", {"sceneName": SCENE_PROGRAM},
            ))
        assert result is None
        assert "TimeoutError" in caplog.text
        assert "SetCurrentProgramScene" in caplog.text
        assert manager._ws is None
        assert socket.closed
    finally:
        manager._ws = previous_ws
        manager._current_scene = previous_scene


def test_switch_scene_keeps_the_current_scene_when_obs_does_not_answer():
    manager = OBSManager()
    previous = manager._current_scene
    manager._current_scene = SCENE_OFFLINE
    original_ensure = manager._ensure_connected
    original_request = manager._request

    async def connected():
        return True

    async def no_answer(_req_type, _req_data=None):
        return None

    manager._ensure_connected = connected
    manager._request = no_answer
    try:
        asyncio.run(manager.switch_scene(SCENE_PROGRAM))
        assert manager._current_scene == SCENE_OFFLINE
    finally:
        manager._ensure_connected = original_ensure
        manager._request = original_request
        manager._current_scene = previous
