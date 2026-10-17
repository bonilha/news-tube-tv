"""YouTube Data API publish times, and the Invidious fallback."""

import asyncio
from datetime import datetime, timezone

import httpx

from app.config import settings
from app.yt import google_yt
from app.yt.invidious import resolve_published


def test_iso_published_at_becomes_unix():
    expected = int(datetime(2026, 10, 16, 18, 30, tzinfo=timezone.utc).timestamp())
    assert google_yt._published_unix("2026-10-16T18:30:00Z") == expected


def test_google_batch_maps_published_at(monkeypatch):
    monkeypatch.setattr(settings, "YOUTUBE_API_KEY", "test-key")

    class _Resp:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "items": [
                    {"id": "abcdefghijk", "snippet": {"publishedAt": "2026-10-16T15:00:00Z"}},
                    {"id": "zzzzzzzzzzz", "snippet": {"publishedAt": "2026-10-16T21:00:00Z"}},
                ]
            }

    class _Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def get(self, url, params=None):
            assert params["key"] == "test-key"
            assert params["id"] == "abcdefghijk,zzzzzzzzzzz"
            return _Resp()

    monkeypatch.setattr(google_yt.httpx, "AsyncClient", _Client)
    found = asyncio.run(google_yt.fetch_videos_published_at(["abcdefghijk", "zzzzzzzzzzz"]))
    assert found["abcdefghijk"] == google_yt._published_unix("2026-10-16T15:00:00Z")
    assert found["zzzzzzzzzzz"] == google_yt._published_unix("2026-10-16T21:00:00Z")


def test_google_http_error_returns_empty(monkeypatch):
    monkeypatch.setattr(settings, "YOUTUBE_API_KEY", "test-key")

    class _Resp:
        def raise_for_status(self):
            request = httpx.Request("GET", "https://www.googleapis.com/youtube/v3/videos")
            response = httpx.Response(403, request=request)
            raise httpx.HTTPStatusError("quota", request=request, response=response)

        def json(self):
            return {}

    class _Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def get(self, url, params=None):
            return _Resp()

    monkeypatch.setattr(google_yt.httpx, "AsyncClient", _Client)
    found = asyncio.run(google_yt.fetch_videos_published_at(["abcdefghijk"]))
    assert found == {}


def test_missing_key_skips_google(monkeypatch):
    monkeypatch.setattr(settings, "YOUTUBE_API_KEY", "")
    found = asyncio.run(google_yt.fetch_videos_published_at(["abcdefghijk"]))
    assert found == {}


def test_one_day_ago_does_not_replace_a_fresh_exact_stamp():
    now = 1_792_250_000
    precise = now - 16 * 3600
    assert resolve_published(precise, "1 dia atrás", now) == precise
    assert resolve_published(precise, "1 day ago", now) == precise
