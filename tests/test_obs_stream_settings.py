"""Stream encoder settings go out through the OBS websocket, from .env values."""

import asyncio

import pytest

from app.config import settings
from app.obs.manager import OBSManager


def _ok(data=None):
    body = {"requestStatus": {"result": True, "code": 100}}
    if data is not None:
        body["responseData"] = data
    return body


@pytest.fixture
def manager():
    obs = OBSManager()
    obs._ws = object()
    return obs


def test_simple_output_uses_env_bitrate(manager, monkeypatch):
    monkeypatch.setattr(settings, "OBS_STREAM_BITRATE_KBPS", 2500)
    monkeypatch.setattr(settings, "OBS_AUDIO_BITRATE_KBPS", 160)
    monkeypatch.setattr(settings, "OBS_KEYFRAME_SEC", 2)
    monkeypatch.setattr(settings, "OBS_BASE_WIDTH", 1920)
    monkeypatch.setattr(settings, "OBS_BASE_HEIGHT", 1080)
    calls = []

    async def request(req_type, req_data=None):
        calls.append((req_type, req_data))
        if req_type == "GetStreamStatus":
            return _ok({"outputActive": False})
        return _ok()

    manager._request = request
    ok, error = asyncio.run(manager.apply_stream_settings())

    assert (ok, error) == (True, "")
    assert ("SetProfileParameter", {
        "parameterCategory": "Output",
        "parameterName": "Mode",
        "parameterValue": "Simple",
    }) in calls
    assert ("SetProfileParameter", {
        "parameterCategory": "SimpleOutput",
        "parameterName": "VBitrate",
        "parameterValue": "2500",
    }) in calls
    assert ("SetProfileParameter", {
        "parameterCategory": "SimpleOutput",
        "parameterName": "ABitrate",
        "parameterValue": "160",
    }) in calls
    video = next(data for kind, data in calls if kind == "SetVideoSettings")
    assert video["videoSettings"]["baseWidth"] == 1920
    assert video["videoSettings"]["outputHeight"] == 1080


def test_active_stream_skips_video_settings(manager):
    calls = []

    async def request(req_type, req_data=None):
        calls.append(req_type)
        return _ok({"outputActive": True})

    manager._request = request
    ok, error = asyncio.run(manager.apply_stream_settings())

    assert (ok, error) == (True, "")
    assert calls == ["GetStreamStatus"]


def test_keyframe_other_than_two_blocks_start(manager, monkeypatch):
    monkeypatch.setattr(settings, "OBS_KEYFRAME_SEC", 4)
    calls = []

    async def request(req_type, req_data=None):
        calls.append(req_type)
        return _ok()

    manager._request = request
    ok, error = asyncio.run(manager.start_streaming())

    assert ok is False
    assert "2" in error
    assert "StartStream" not in calls
