"""Session cookie without per-request middleware, log clock, 24h protect, ffmpeg cap."""

import asyncio
import subprocess
from datetime import datetime
from pathlib import Path

from fastapi import Depends, FastAPI
from fastapi.responses import HTMLResponse
from fastapi.testclient import TestClient

from app.auth import SessionCookie, verify_credentials
from app.config import settings
from app.cycle import ids_to_protect
from app.queue.download import (
    _FFMPEG_TIMEOUT_SEC,
    _measure_loudness,
    _run_ffmpeg,
    protect_video_ids,
    purge_expired,
    say,
)
from main import format_heartbeat


def test_say_prints_the_local_clock(capsys):
    say("Baixando um video", now=datetime(2026, 10, 17, 14, 5, 9))
    assert capsys.readouterr().out == "NewsTube 2026-10-17 14:05:09 Baixando um video\n"


def test_heartbeat_names_uptime_and_pending_tasks():
    line = format_heartbeat(
        uptime_sec=57 * 3600 + 5 * 60,
        cycle_on=True,
        streaming=False,
        download_busy=True,
        oldest_hours=30.5,
        pending_tasks=12,
    )
    assert line == (
        "Vivo ha 57h05m | ciclo ligado | RTMP parado | "
        "download preso | pendente mais antigo 30.5h | tarefas 12"
    )


def test_a_video_past_24h_is_not_protected_unless_it_is_on_air():
    now = 1_800_000_000
    fresh = {"video_id": "fresh000001", "published_unix": now - 3600}
    stale = {"video_id": "stale000001", "published_unix": now - 90000}
    # published_too_old reads the wall clock, so pin it.
    import app.queue.download as download

    original = download.published_cutoff
    download.published_cutoff = lambda now_unix=None: now - 86400
    try:
        assert ids_to_protect([fresh, stale], "") == {"fresh000001"}
        assert ids_to_protect([fresh, stale], "stale000001") == {"fresh000001", "stale000001"}
    finally:
        download.published_cutoff = original


def test_purge_drops_an_old_pending_row_and_keeps_the_one_on_air(monkeypatch, tmp_path, capsys):
    from app.database import get_db, init_db

    monkeypatch.setattr(settings, "DB_PATH", tmp_path / "age.db")
    now = int(datetime.now().timestamp())

    async def run():
        await init_db()
        async with get_db() as db:
            await db.execute(
                "INSERT INTO channels (name, channel_id) VALUES (?, ?)",
                ("Canal", "UCage"),
            )
            cursor = await db.execute("SELECT id FROM channels")
            channel_id = (await cursor.fetchone())[0]
            for video_id, title in (
                ("oldvideo001", "Video velho"),
                ("onair000001", "No ar"),
            ):
                await db.execute(
                    """INSERT INTO queue
                       (channel_id, video_id, title, author, duration, published_unix, status)
                       VALUES (?, ?, ?, ?, ?, ?, 'pending')""",
                    (channel_id, video_id, title, "autor", 60, now - 100000),
                )
            await db.commit()
        protect_video_ids({"onair000001"})
        try:
            removed = await purge_expired()
            async with get_db() as db:
                cursor = await db.execute("SELECT video_id FROM queue ORDER BY video_id")
                left = [row[0] for row in await cursor.fetchall()]
        finally:
            protect_video_ids(set())
        return removed, left

    removed, left = asyncio.run(run())
    assert removed == 1
    assert left == ["onair000001"]
    printed = capsys.readouterr().out
    assert "Fora das 24h" in printed
    assert "Video velho" in printed


def test_ffmpeg_encode_and_measure_give_up_after_half_an_hour(monkeypatch, tmp_path):
    seen: dict = {}

    def fake_run(cmd, capture_output, timeout):
        seen["timeout"] = timeout
        dest = Path(cmd[-1])
        dest.write_bytes(b"mp4")
        completed = subprocess.CompletedProcess(cmd, 0, b"", b"")
        return completed

    monkeypatch.setattr("app.queue.download.subprocess.run", fake_run)
    _run_ffmpeg(tmp_path / "in.mp4", tmp_path / "out.mp4", ["-c", "copy"])
    assert seen["timeout"] == _FFMPEG_TIMEOUT_SEC == 30 * 60

    def timed_out(cmd, capture_output, timeout):
        raise subprocess.TimeoutExpired(cmd, timeout)

    monkeypatch.setattr("app.queue.download.subprocess.run", timed_out)
    assert _measure_loudness(tmp_path / "in.mp4") is None
    try:
        _run_ffmpeg(tmp_path / "in.mp4", tmp_path / "late.mp4", ["-c", "copy"])
    except RuntimeError as exc:
        assert "ffmpeg failed" in str(exc)
    else:
        raise AssertionError("a timed-out encode must fail the file")


def test_basic_login_sets_the_session_cookie_and_the_cookie_is_enough():
    app = FastAPI()
    app.add_middleware(SessionCookie)

    @app.get("/ping")
    async def ping(_auth: bool = Depends(verify_credentials)):
        return {"ok": True}

    @app.get("/page", response_class=HTMLResponse)
    async def page(_auth: bool = Depends(verify_credentials)):
        return HTMLResponse("<p>ok</p>")

    client = TestClient(app)
    missing = client.get("/ping")
    assert missing.status_code == 401

    logged = client.get("/ping", auth=(settings.ADMIN_USER, settings.ADMIN_PASSWORD))
    assert logged.status_code == 200
    cookie = logged.cookies.get("nt_session")
    assert cookie

    page = client.get("/page", auth=(settings.ADMIN_USER, settings.ADMIN_PASSWORD))
    assert page.status_code == 200
    assert page.cookies.get("nt_session") == cookie

    again = client.get("/ping", cookies={"nt_session": cookie})
    assert again.status_code == 200
