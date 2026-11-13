"""Saved typography settings for the public radar page."""
from __future__ import annotations

from app.database import get_db

RADAR_FONT_FAMILIES = (
    "Arial",
    "Arial Black",
    "Calibri",
    "Courier New",
    "Georgia",
    "Manrope",
    "Tahoma",
    "Times New Roman",
    "Trebuchet MS",
    "Unbounded",
    "Verdana",
)

async def get_radar_style() -> dict:
    async with get_db() as db:
        cursor = await db.execute(
            """SELECT title_font_family, title_font_size, channel_font_family, channel_font_size
               FROM radar_display_config WHERE id = 1"""
        )
        row = await cursor.fetchone()
    if row is None:
        raise RuntimeError("Radar typography settings were not initialized.")
    return dict(row)


async def save_radar_style(
    *,
    title_font_family: str,
    title_font_size: int,
    channel_font_family: str,
    channel_font_size: int,
) -> None:
    if title_font_family not in RADAR_FONT_FAMILIES:
        raise ValueError("Selecione uma fonte válida para o título.")
    if channel_font_family not in RADAR_FONT_FAMILIES:
        raise ValueError("Selecione uma fonte válida para o canal.")
    if not 24 <= title_font_size <= 72:
        raise ValueError("O tamanho do título deve estar entre 24 e 72 px.")
    if not 14 <= channel_font_size <= 40:
        raise ValueError("O tamanho do canal deve estar entre 14 e 40 px.")

    async with get_db() as db:
        await db.execute(
            """INSERT INTO radar_display_config
               (id, title_font_family, title_font_size, channel_font_family, channel_font_size)
               VALUES (1, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET
                   title_font_family = excluded.title_font_family,
                   title_font_size = excluded.title_font_size,
                   channel_font_family = excluded.channel_font_family,
                   channel_font_size = excluded.channel_font_size""",
            (title_font_family, title_font_size, channel_font_family, channel_font_size),
        )
        await db.commit()
