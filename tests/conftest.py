"""Keep the suite off the YouTube Data API. Live checks set the key themselves."""

import pytest

from app.config import settings


@pytest.fixture(autouse=True)
def _youtube_key_off(monkeypatch):
    monkeypatch.setattr(settings, "YOUTUBE_API_KEY", "")
