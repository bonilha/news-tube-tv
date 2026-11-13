import asyncio

import pytest


def test_radar_style_defaults_and_saved_values(monkeypatch, tmp_path):
    from app.config import settings
    from app.database import init_db
    from app.radar_style import get_radar_style, save_radar_style

    monkeypatch.setattr(settings, "DB_PATH", tmp_path / "radar-style.db")

    async def run():
        await init_db()
        assert await get_radar_style() == {
            "title_font_family": "Manrope",
            "title_font_size": 48,
            "channel_font_family": "Manrope",
            "channel_font_size": 26,
        }
        await save_radar_style(
            title_font_family="Georgia",
            title_font_size=54,
            channel_font_family="Arial",
            channel_font_size=30,
        )
        assert await get_radar_style() == {
            "title_font_family": "Georgia",
            "title_font_size": 54,
            "channel_font_family": "Arial",
            "channel_font_size": 30,
        }

    asyncio.run(run())


def test_public_radar_display_config_returns_saved_style(monkeypatch, tmp_path):
    from app.config import settings
    from app.database import init_db
    from app.radar_style import save_radar_style
    from app.routes.radar import radar_display_config

    monkeypatch.setattr(settings, "DB_PATH", tmp_path / "radar-api.db")

    async def run():
        await init_db()
        await save_radar_style(
            title_font_family="Georgia",
            title_font_size=54,
            channel_font_family="Arial",
            channel_font_size=30,
        )
        response = await radar_display_config()
        assert response.headers["Cache-Control"] == "no-store"
        assert response.body == (
            b'{"title_font_family":"Georgia","title_font_size":54,'
            b'"channel_font_family":"Arial","channel_font_size":30}'
        )

    asyncio.run(run())


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("title_font_size", 23),
        ("channel_font_size", 41),
        ("title_font_family", "not-a-font"),
        ("channel_font_family", "not-a-font"),
    ],
)
def test_radar_style_rejects_invalid_values(field, value):
    from app.radar_style import save_radar_style

    values = {
        "title_font_family": "Manrope",
        "title_font_size": 48,
        "channel_font_family": "Manrope",
        "channel_font_size": 26,
    }
    values[field] = value

    with pytest.raises(ValueError):
        asyncio.run(save_radar_style(**values))
