"""Loudness normalization is on for new downloads and can be turned off."""

import asyncio

from fastapi.templating import Jinja2Templates

from app.queue.download import loudnorm_measure_args, placement_action

MEASURED = {
    "input_i": "-20.5",
    "input_tp": "-3.2",
    "input_lra": "8.1",
    "input_thresh": "-31.0",
    "target_offset": "6.5",
}


def test_measure_pass_does_not_encode_video():
    args = loudnorm_measure_args()
    assert args[0] == "-vn"
    assert "loudnorm=I=-14:TP=-1.5:LRA=11:print_format=json" in args
    assert "-c:v" not in args
    assert "libx264" not in args
    assert "aac" not in args


def test_enabled_h264_aac_is_copied_video_and_normalized_audio():
    args = placement_action(
        video_codec="h264",
        audio_codec="aac",
        suffix=".mp4",
        needs_scale=False,
        normalize=True,
        measured=MEASURED,
        encoder="libx264",
        encoder_args=("-preset", "veryfast"),
        scale_filter="scale=1920:1080",
    )
    assert args[:4] == ["-c:v", "copy", "-af", args[3]]
    assert "linear=true" in args[3]
    assert "measured_I=-20.5" in args[3]
    assert "offset=6.5" in args[3]
    assert args[-4:] == ["-c:a", "aac", "-b:a", "160k"]
    assert "libx264" not in args


def test_disabled_h264_aac_on_canvas_is_only_moved():
    assert placement_action(
        video_codec="h264",
        audio_codec="aac",
        suffix=".mp4",
        needs_scale=False,
        normalize=False,
        measured=None,
        encoder="libx264",
        encoder_args=(),
        scale_filter="scale=1920:1080",
    ) == "move"


def test_a_new_database_normalizes_audio(monkeypatch, tmp_path):
    from app.broadcast import audio_normalize_enabled, get_control, save_audio_normalize
    from app.config import settings
    from app.database import init_db

    monkeypatch.setattr(settings, "DB_PATH", tmp_path / "loudnorm.db")

    async def run():
        await init_db()
        row = await get_control()
        assert row["audio_normalize"] == 1
        assert await audio_normalize_enabled() is True
        await save_audio_normalize(False)
        assert await audio_normalize_enabled() is False

    asyncio.run(run())


def test_configs_checkbox_follows_the_saved_flag():
    templates = Jinja2Templates(directory="app/templates")
    html = templates.env.get_template("configs.html").render(
        htmx=True,
        config={"mode": "custom", "service": "", "region": "", "stream_key": "", "server_url": "", "services_path": ""},
        services=[],
        services_found=False,
        services_missing=False,
        services_json="[]",
        cookie_state={"ok": True, "message": ""},
        cookie_present=False,
        control={
            "schedule_mode": "off",
            "schedule_time": "",
            "schedule_date": "",
            "audio_normalize": 1,
            "limit_seconds": 0,
        },
        saved=False,
    )
    assert 'name="audio_normalize"' in html
    assert "Normalizar áudio" in html
    assert "checked" in html.split("Normalizar áudio")[0][-80:]
