import asyncio

import httpx

from app import radar_data
from app.cycle import cycle_manager
from app.routes import overlay as overlay_routes


def test_yahoo_chart_is_normalized_to_quote_change_and_series():
    def handle(request):
        assert request.url.host == "query2.finance.yahoo.com"
        assert request.url.params["interval"] == "15m"
        payload = {
            "chart": {
                "result": [{
                    "meta": {"regularMarketPrice": 25, "previousClose": 20},
                    "indicators": {"quote": [{"close": [20, None, 25]}]},
                }],
            },
        }
        return httpx.Response(200, json=payload)

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            return await radar_data.fetch_yahoo_symbol(client, "^BVSP")

    assert asyncio.run(run()) == {"p": 25, "c": 25, "s": [20, 25]}


def _chart(price, previous, closes):
    return {
        "chart": {
            "result": [{
                "meta": {"regularMarketPrice": price, "previousClose": previous},
                "indicators": {"quote": [{"close": closes}]},
            }],
        },
    }


def test_yahoo_keeps_last_close_when_the_session_has_no_candles():
    def handle(request):
        assert request.url.params["interval"] == "15m"
        if request.url.params["range"] == "1d":
            return httpx.Response(200, json=_chart(172457.77, 171000, []))
        assert request.url.params["range"] == "5d"
        return httpx.Response(200, json=_chart(1, 1, [170000, 171000, 172457.77]))

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            return await radar_data.fetch_yahoo_symbol(client, "^BVSP")

    result = asyncio.run(run())
    assert result["p"] == 172457.77
    assert result["s"] == [170000, 171000, 172457.77]
    assert result["c"] != 0


def test_yahoo_returns_price_when_no_range_has_candles():
    def handle(request):
        return httpx.Response(200, json=_chart(172457.77, 172457.77, []))

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            return await radar_data.fetch_yahoo_symbol(client, "^BVSP")

    assert asyncio.run(run()) == {"p": 172457.77, "c": 0, "s": []}


def test_weather_fetch_normalizes_all_requested_cities():
    def handle(request):
        assert request.url.host == "api.open-meteo.com"
        assert len(request.url.params["latitude"].split(",")) == len(radar_data._CITIES)
        return httpx.Response(200, json=[
            {"current": {"temperature_2m": 22.5, "weather_code": 2}}
            for _ in radar_data._CITIES
        ])

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            return await radar_data.fetch_weather(client)

    result = asyncio.run(run())
    assert len(result) == 11
    assert result["Curitiba"] == {"t": 22.5, "c": 2}


def _yahoo_chart(price=25):
    return {
        "chart": {
            "result": [{
                "meta": {"regularMarketPrice": price, "previousClose": 20},
                "indicators": {"quote": [{"close": [20, price]}]},
            }],
        },
    }


def _patch_market_client(monkeypatch, handle):
    client_cls = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handle)
        return client_cls(*args, **kwargs)

    monkeypatch.setattr(radar_data.httpx, "AsyncClient", factory)
    monkeypatch.setattr(radar_data, "_market_cache", None)
    monkeypatch.setattr(radar_data, "_market_expires_at", 0.0)
    monkeypatch.setattr(radar_data.settings, "AWESOMEAPI_KEY", "test-key")


def test_awesome_quotes_use_the_key_and_stay_cached(monkeypatch):
    awesome_calls = 0

    def handle(request):
        nonlocal awesome_calls
        if request.url.host == "economia.awesomeapi.com.br":
            awesome_calls += 1
            assert request.headers["x-api-key"] == "test-key"
            return httpx.Response(200, json={
                "USDBRL": {"bid": "5.5", "pctChange": "1.25"},
                "EURBRL": {"bid": "6.1", "pctChange": "-0.4"},
                "BTCUSD": {"bid": "100", "pctChange": "2"},
                "ETHUSD": {"bid": "50", "pctChange": "3"},
            })
        return httpx.Response(200, json=_yahoo_chart())

    _patch_market_client(monkeypatch, handle)

    async def run():
        first = await radar_data.market_snapshot()
        second = await radar_data.market_snapshot()
        return first, second

    first, second = asyncio.run(run())
    assert awesome_calls == 1
    assert first["data"]["USD"]["p"] == 5.5
    assert first["data"]["USD"]["c"] == 1.25
    assert second == first


def test_awesome_429_keeps_quotes_until_the_cache_expires(monkeypatch):
    awesome_calls = 0

    def handle(request):
        nonlocal awesome_calls
        if request.url.host == "economia.awesomeapi.com.br":
            awesome_calls += 1
            return httpx.Response(429, json={"message": "slow down"})
        return httpx.Response(200, json=_yahoo_chart(30))

    _patch_market_client(monkeypatch, handle)
    monkeypatch.setattr(radar_data, "_market_cache", {
        "data": {"USD": {"p": 5.0, "c": 0.1, "s": [4, 5]}},
        "updated_at": "2026-11-13T12:00:00+00:00",
        "stale": False,
        "errors": [],
    })

    async def run():
        failed = await radar_data.market_snapshot()
        cached = await radar_data.market_snapshot()
        return failed, cached

    failed, cached = asyncio.run(run())
    assert awesome_calls == 1
    assert failed["data"]["USD"]["p"] == 30
    assert failed["stale"] is True
    assert any(item.startswith("AwesomeAPI:") for item in failed["errors"])
    assert cached == failed
    assert radar_data._market_expires_at > 0


def test_market_snapshot_keeps_last_successful_data_and_marks_it_stale(monkeypatch):
    previous = {
        "data": {"IBOV": {"p": 120000, "c": 0.5, "s": [119000, 120000]}},
        "updated_at": "2026-11-12T12:00:00+00:00",
        "stale": False,
        "errors": [],
    }

    async def failed_refresh():
        return {}, ["Yahoo Finance indisponível"]

    monkeypatch.setattr(radar_data, "_market_cache", previous)
    monkeypatch.setattr(radar_data, "_market_expires_at", 0)
    monkeypatch.setattr(radar_data, "_market_refresh", failed_refresh)
    snapshot = asyncio.run(radar_data.market_snapshot())

    assert snapshot["data"] == previous["data"]
    assert snapshot["updated_at"] == previous["updated_at"]
    assert snapshot["stale"] is True
    assert snapshot["errors"] == ["Yahoo Finance indisponível"]


def test_overlay_now_uses_metadata_for_the_current_cycle_video(monkeypatch):
    expected = {
        "originalVideoUrl": "https://www.youtube.com/watch?v=current-video",
        "videoTitle": "Vídeo atual",
        "channelName": "Canal atual",
        "publishedLine": "",
    }

    async def video_payload(video_id):
        assert video_id == "current-video"
        return expected

    monkeypatch.setattr(cycle_manager, "status_dict", lambda: {"current_video_id": "current-video"})
    monkeypatch.setattr(overlay_routes.overlay, "video_payload", video_payload)

    assert asyncio.run(overlay_routes.overlay_now()) == expected
