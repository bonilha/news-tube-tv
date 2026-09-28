from __future__ import annotations

import aiosqlite
from contextlib import asynccontextmanager
from typing import AsyncIterator

from app.config import settings

_DDL = """
CREATE TABLE IF NOT EXISTS assets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    filename TEXT NOT NULL UNIQUE,
    original_name TEXT NOT NULL,
    mime_type TEXT NOT NULL,
    size INTEGER NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS channels (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    channel_id TEXT NOT NULL UNIQUE,
    handle TEXT NOT NULL DEFAULT '',
    min_age_hours REAL NOT NULL DEFAULT 2.0,
    active INTEGER NOT NULL DEFAULT 1,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id INTEGER NOT NULL,
    video_id TEXT NOT NULL,
    title TEXT NOT NULL,
    author TEXT NOT NULL,
    duration INTEGER NOT NULL DEFAULT 0,
    published_unix INTEGER NOT NULL,
    thumb TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending',
    added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    play_order INTEGER NOT NULL DEFAULT 0,
    play_count INTEGER NOT NULL DEFAULT 0,
    max_height INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (channel_id) REFERENCES channels(id) ON DELETE CASCADE,
    UNIQUE(video_id)
);
"""


@asynccontextmanager
async def get_db() -> AsyncIterator[aiosqlite.Connection]:
    db = await aiosqlite.connect(str(settings.DB_PATH))
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA foreign_keys = ON")
    try:
        yield db
    finally:
        await db.close()


async def init_db() -> None:
    async with get_db() as db:
        await db.executescript(_DDL)
        # Rows left behind when a channel was deleted with foreign keys off.
        await db.execute(
            "DELETE FROM queue WHERE channel_id NOT IN (SELECT id FROM channels)"
        )
        cursor = await db.execute("PRAGMA table_info(queue)")
        columns = {row[1] for row in await cursor.fetchall()}
        if "local_path" not in columns:
            await db.execute(
                "ALTER TABLE queue ADD COLUMN local_path TEXT NOT NULL DEFAULT ''"
            )
        if "download_error" not in columns:
            await db.execute(
                "ALTER TABLE queue ADD COLUMN download_error TEXT NOT NULL DEFAULT ''"
            )
        if "play_order" not in columns:
            await db.execute(
                "ALTER TABLE queue ADD COLUMN play_order INTEGER NOT NULL DEFAULT 0"
            )
        if "play_count" not in columns:
            await db.execute(
                "ALTER TABLE queue ADD COLUMN play_count INTEGER NOT NULL DEFAULT 0"
            )
        if "max_height" not in columns:
            await db.execute(
                "ALTER TABLE queue ADD COLUMN max_height INTEGER NOT NULL DEFAULT 0"
            )
        await db.commit()


async def insert_asset(
    *, filename: str, original_name: str, mime_type: str, size: int
) -> int:
    async with get_db() as db:
        cursor = await db.execute(
            "INSERT INTO assets (filename, original_name, mime_type, size) VALUES (?, ?, ?, ?)",
            (filename, original_name, mime_type, size),
        )
        await db.commit()
        return cursor.lastrowid


async def list_assets() -> list[dict]:
    async with get_db() as db:
        cursor = await db.execute(
            "SELECT id, filename, original_name, mime_type, size, created_at "
            "FROM assets ORDER BY created_at DESC"
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]


async def get_asset(asset_id: int) -> dict | None:
    async with get_db() as db:
        cursor = await db.execute(
            "SELECT id, filename, original_name, mime_type, size, created_at "
            "FROM assets WHERE id = ?",
            (asset_id,),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None


async def delete_asset(asset_id: int) -> dict | None:
    async with get_db() as db:
        cursor = await db.execute(
            "SELECT id, filename, original_name, mime_type, size, created_at "
            "FROM assets WHERE id = ?",
            (asset_id,),
        )
        row = await cursor.fetchone()
        if row:
            asset = dict(row)
            await db.execute("DELETE FROM assets WHERE id = ?", (asset_id,))
            await db.commit()
            return asset
        return None
