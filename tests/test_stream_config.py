"""Preset RTMP uses the OBS service name and region. Custom keeps the URL."""

import json

from app.obs.services import candidate_paths, load_services, locate_services_file
from app.obs.stream_config import service_payload

TWITTER = {
    "name": "Twitter",
    "alt_names": ["X"],
    "servers": [
        {"name": "South America: São Paulo, Brazil", "url": "rtmps://br.pscp.tv:443/x"},
        {"name": "US West: San Francisco, CA", "url": "rtmps://ca.pscp.tv:443/x"},
    ],
}


def test_preset_sends_the_region_name_not_the_url():
    built = service_payload(
        {"mode": "preset", "service": "X", "region": "South America: São Paulo, Brazil", "stream_key": "abc"},
        [TWITTER],
    )
    assert built == (
        "rtmp_common",
        {"service": "Twitter", "key": "abc", "server": "South America: São Paulo, Brazil"},
    )


def test_preset_without_a_region_does_not_start():
    assert service_payload(
        {"mode": "preset", "service": "Twitter", "region": "", "stream_key": "abc"},
        [TWITTER],
    ) == "Escolha a região em Configs."


def test_one_server_ignores_a_blank_region():
    solo = {"name": "Example", "alt_names": [], "servers": [{"name": "Primary", "url": "rtmp://example/live"}]}
    kind, payload = service_payload(
        {"mode": "preset", "service": "Example", "region": "", "stream_key": "abc"},
        [solo],
    )
    assert kind == "rtmp_common"
    assert payload["server"] == "Primary"


def test_custom_uses_the_url():
    kind, payload = service_payload(
        {"mode": "custom", "server_url": "rtmp://localhost/live", "stream_key": "abc"},
        [],
    )
    assert kind == "rtmp_custom"
    assert payload["server"] == "rtmp://localhost/live"


def test_install_locations_can_be_listed():
    paths = candidate_paths()
    assert paths
    assert all(path.name == "services.json" for path in paths)


def test_saved_path_is_the_services_file(monkeypatch, tmp_path):
    from app.config import settings
    catalog = tmp_path / "services.json"
    catalog.write_text(json.dumps({"services": [TWITTER]}), encoding="utf-8")
    monkeypatch.setattr(settings, "OBS_SERVICES_JSON", "")
    assert locate_services_file(str(catalog)) == catalog
    assert load_services(catalog)[0]["name"] == "Twitter"


def test_missing_saved_path_does_not_fall_through(monkeypatch, tmp_path):
    from app.config import settings
    monkeypatch.setattr(settings, "OBS_SERVICES_JSON", str(tmp_path / "other.json"))
    assert locate_services_file(str(tmp_path / "absent.json")) is None
