"""Public browser-source page and data for the live radar."""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, JSONResponse

from app.radar_data import RadarDataUnavailable, market_snapshot, weather_snapshot
from app.radar_style import get_radar_style

router = APIRouter(tags=["radar"])


@router.get("/radar", include_in_schema=False)
async def radar_page():
    return FileResponse(Path("static/live-top24h.html"), media_type="text/html")


@router.get("/api/radar/markets")
async def radar_markets():
    try:
        return JSONResponse(await market_snapshot(), headers={"Cache-Control": "no-store"})
    except RadarDataUnavailable as exc:
        raise HTTPException(
            status_code=503,
            detail={"message": str(exc), "errors": exc.errors},
        ) from exc


@router.get("/api/radar/weather")
async def radar_weather():
    try:
        return JSONResponse(await weather_snapshot(), headers={"Cache-Control": "no-store"})
    except RadarDataUnavailable as exc:
        raise HTTPException(
            status_code=503,
            detail={"message": "Nenhuma fonte pública de clima respondeu.", "errors": exc.errors},
        ) from exc


@router.get("/api/radar/display-config")
async def radar_display_config():
    return JSONResponse(await get_radar_style(), headers={"Cache-Control": "no-store"})
