"""Public market and weather snapshots for the live radar page."""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote

import httpx

from app.config import settings

log = logging.getLogger(__name__)

REFRESH_SECONDS = 300
CACHE_SECONDS = REFRESH_SECONDS - 5
_HTTP_TIMEOUT = 12.0

MARKET_SYMBOLS = {
    "IBOV": "^BVSP",
    "USD": "BRL=X",
    "EUR": "EURBRL=X",
    "BTC": "BTC-USD",
    "ETH": "ETH-USD",
    "IXIC": "^IXIC",
    "GSPC": "^GSPC",
}

_CITIES = (
    ("Curitiba", -25.43, -49.27),
    ("Florianópolis", -27.60, -48.55),
    ("Porto Alegre", -30.03, -51.22),
    ("São Paulo", -23.55, -46.63),
    ("Rio de Janeiro", -22.91, -43.17),
    ("Belo Horizonte", -19.92, -43.94),
    ("Vitória", -20.32, -40.34),
    ("Brasília", -15.79, -47.88),
    ("Goiânia", -16.68, -49.25),
    ("Campo Grande", -20.47, -54.62),
    ("Cuiabá", -15.60, -56.10),
)

_market_cache: dict[str, Any] | None = None
_weather_cache: dict[str, Any] | None = None
_market_expires_at = 0.0
_weather_expires_at = 0.0
_market_lock = asyncio.Lock()
_weather_lock = asyncio.Lock()
_awesome_key_warned = False


class RadarDataUnavailable(Exception):
    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__("Nenhuma fonte pública de dados financeiros respondeu.")


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if result == result and abs(result) != float("inf") else None


def _updated_at() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _chart_series(payload: dict[str, Any]) -> list[float]:
    result = payload["chart"]["result"][0]
    quote_rows = (result.get("indicators") or {}).get("quote") or [{}]
    closes = quote_rows[0].get("close") or []
    return [value for item in closes if (value := _number(item)) is not None]


async def fetch_yahoo_symbol(client: httpx.AsyncClient, symbol: str) -> dict[str, Any]:
    url = f"https://query2.finance.yahoo.com/v8/finance/chart/{quote(symbol, safe='')}"
    response = await client.get(url, params={"interval": "15m", "range": "1d"})
    response.raise_for_status()
    payload = response.json()
    result = payload["chart"]["result"][0]
    meta = result["meta"]
    price = _number(meta.get("regularMarketPrice"))
    previous = _number(meta.get("previousClose")) or _number(meta.get("chartPreviousClose"))
    if price is None or previous is None or previous == 0:
        raise ValueError(f"Yahoo Finance não retornou cotação válida para {symbol}.")
    series = _chart_series(payload)
    # Sem pregão (fim de semana, feriado ou antes da abertura) o range de 1 dia
    # volta sem candles. O preço do último fechamento continua válido.
    if len(series) < 2:
        try:
            wider = await client.get(url, params={"interval": "15m", "range": "5d"})
            wider.raise_for_status()
            series = _chart_series(wider.json())
        except (httpx.HTTPError, ValueError, TypeError, KeyError, IndexError):
            series = series if series else []
    return {
        "p": price,
        "c": (price / previous - 1) * 100,
        "s": series,
    }


async def fetch_awesome_quotes(
    client: httpx.AsyncClient, api_key: str
) -> tuple[dict[str, dict[str, float]], list[str]]:
    response = await client.get(
        "https://economia.awesomeapi.com.br/last/USD-BRL,EUR-BRL,BTC-USD,ETH-USD",
        headers={"x-api-key": api_key},
    )
    response.raise_for_status()
    payload = response.json()
    result: dict[str, dict[str, float]] = {}
    errors = []
    for key, code in (("USD", "USDBRL"), ("EUR", "EURBRL"), ("BTC", "BTCUSD"), ("ETH", "ETHUSD")):
        pair = payload.get(code) or {}
        price = _number(pair.get("bid"))
        change = _number(pair.get("pctChange"))
        if price is None:
            errors.append(f"AwesomeAPI não retornou cotação válida para {code}.")
            continue
        result[key] = {"p": price}
        if change is not None:
            result[key]["c"] = change
    return result, errors


async def fetch_weather(client: httpx.AsyncClient) -> dict[str, dict[str, float | int]]:
    params = {
        "latitude": ",".join(str(city[1]) for city in _CITIES),
        "longitude": ",".join(str(city[2]) for city in _CITIES),
        "current": "temperature_2m,weather_code",
        "temperature_unit": "celsius",
        "timezone": "America/Sao_Paulo",
    }
    response = await client.get("https://api.open-meteo.com/v1/forecast", params=params)
    response.raise_for_status()
    payload = response.json()
    locations = payload if isinstance(payload, list) else [payload]
    if len(locations) != len(_CITIES):
        raise ValueError("Open-Meteo retornou uma quantidade inesperada de cidades.")

    result = {}
    for (name, _, _), location in zip(_CITIES, locations):
        current = location.get("current") or {}
        temperature = _number(current.get("temperature_2m"))
        weather_code = _number(current.get("weather_code"))
        if temperature is None or weather_code is None:
            raise ValueError(f"Open-Meteo não retornou dados atuais para {name}.")
        result[name] = {"t": temperature, "c": int(weather_code)}
    return result


async def _market_refresh() -> tuple[dict[str, dict[str, Any]], list[str]]:
    errors: list[str] = []
    data: dict[str, dict[str, Any]] = {}
    async with httpx.AsyncClient(
        timeout=_HTTP_TIMEOUT,
        headers={"User-Agent": "NewsTubeTV/1.0"},
    ) as client:
        yahoo_results = await asyncio.gather(
            *(fetch_yahoo_symbol(client, symbol) for symbol in MARKET_SYMBOLS.values()),
            return_exceptions=True,
        )
        for key, symbol, result in zip(MARKET_SYMBOLS, MARKET_SYMBOLS.values(), yahoo_results):
            if isinstance(result, Exception):
                errors.append(f"Yahoo Finance ({symbol}): {result}")
                log.warning("Radar quote refresh failed for %s: %s", symbol, result)
            else:
                data[key] = result

        await _overlay_awesome_quotes(client, data, errors)
    return data, errors


async def _overlay_awesome_quotes(
    client: httpx.AsyncClient,
    data: dict[str, dict[str, Any]],
    errors: list[str],
) -> None:
    global _awesome_key_warned
    api_key = settings.AWESOMEAPI_KEY.strip()
    if not api_key:
        if not _awesome_key_warned:
            _awesome_key_warned = True
            log.warning("Radar exchange quotes skipped: AWESOMEAPI_KEY is empty.")
        return
    try:
        live_quotes, quote_errors = await fetch_awesome_quotes(client, api_key)
        errors.extend(quote_errors)
        for key, quote_data in live_quotes.items():
            data.setdefault(key, {}).update(quote_data)
    except (httpx.HTTPError, ValueError, TypeError) as exc:
        errors.append(f"AwesomeAPI: {exc}")
        log.warning("Radar exchange quote refresh failed: %s", exc)


async def market_snapshot() -> dict[str, Any]:
    global _market_cache, _market_expires_at
    if _market_cache is not None and time.monotonic() < _market_expires_at:
        return _market_cache

    async with _market_lock:
        if _market_cache is not None and time.monotonic() < _market_expires_at:
            return _market_cache
        fresh_data, errors = await _market_refresh()
        has_fresh_data = bool(fresh_data)
        if _market_cache is not None:
            merged = {key: dict(value) for key, value in _market_cache["data"].items()}
            for key, value in fresh_data.items():
                merged.setdefault(key, {}).update(value)
            fresh_data = merged
        if not has_fresh_data:
            if _market_cache is None:
                raise RadarDataUnavailable(errors)
            _market_expires_at = time.monotonic() + CACHE_SECONDS
            return {**_market_cache, "stale": True, "errors": errors}

        _market_cache = {
            "data": fresh_data,
            "updated_at": _updated_at(),
            "stale": bool(errors),
            "errors": errors,
        }
        _market_expires_at = time.monotonic() + CACHE_SECONDS
        return _market_cache


async def weather_snapshot() -> dict[str, Any]:
    global _weather_cache, _weather_expires_at
    if _weather_cache is not None and time.monotonic() < _weather_expires_at:
        return _weather_cache

    async with _weather_lock:
        if _weather_cache is not None and time.monotonic() < _weather_expires_at:
            return _weather_cache
        try:
            async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
                fresh_data = await fetch_weather(client)
        except (httpx.HTTPError, ValueError, TypeError, KeyError) as exc:
            log.warning("Radar weather refresh failed: %s", exc)
            if _weather_cache is None:
                raise RadarDataUnavailable([f"Open-Meteo: {exc}"]) from exc
            _weather_expires_at = time.monotonic() + CACHE_SECONDS
            return {
                **_weather_cache,
                "stale": True,
                "errors": [f"Open-Meteo: {exc}"],
            }

        _weather_cache = {
            "data": fresh_data,
            "updated_at": _updated_at(),
            "stale": False,
            "errors": [],
        }
        _weather_expires_at = time.monotonic() + CACHE_SECONDS
        return _weather_cache
