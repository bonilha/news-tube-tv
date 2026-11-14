"""Canonical OBS scenes and sources are created only from Configurações."""

import asyncio

from app.cycle import PLAYER_SOURCE, CycleManager, SCENE_PROGRAM
from app.obs.manager import ALL_SCENES
from app.obs.setup import provision_obs
from app.overlay.service import OVERLAY_SOURCES


class FakeObs:
    def __init__(self):
        self._ws = object()
        self.scenes = []
        self.inputs = []
        self.items = []
        self.created_inputs = []
        self.created_items = []

    async def connect(self):
        return True

    async def get_scene_list(self):
        return [{"sceneName": name} for name in self.scenes]

    async def _ensure_scenes(self):
        for name in ALL_SCENES:
            if name not in self.scenes:
                self.scenes.append(name)

    async def get_input_list(self):
        return [{"inputName": name} for name in self.inputs]

    async def get_scene_items(self, scene_name):
        assert scene_name == SCENE_PROGRAM
        return [{"sourceName": name} for name in self.items]

    async def create_input(self, name, kind, settings, scene_name=None):
        self.created_inputs.append(name)
        self.inputs.append(name)
        self.items.append(name)
        return {"sceneItemId": 1}

    async def create_scene_item(self, scene_name, source_name):
        self.created_items.append(source_name)
        self.items.append(source_name)
        return {"sceneItemId": 2}


def test_provision_creates_once_and_skips_what_exists(monkeypatch):
    obs = FakeObs()
    monkeypatch.setattr("app.obs.setup.obs_manager", obs)

    async def ensure_overlay():
        for name in OVERLAY_SOURCES:
            if name not in obs.inputs:
                obs.inputs.append(name)
                obs.items.append(name)

    monkeypatch.setattr("app.obs.setup.ensure_overlay", ensure_overlay)

    first = asyncio.run(provision_obs())
    assert first["ok"] is True
    assert first["created_scenes"] == ALL_SCENES
    assert first["player"] == "criada"
    assert first["created_overlay"] == list(OVERLAY_SOURCES)
    assert obs.created_inputs == [PLAYER_SOURCE]

    second = asyncio.run(provision_obs())
    assert second["created_scenes"] == []
    assert second["player"] == ""
    assert second["created_overlay"] == []
    assert obs.created_inputs == [PLAYER_SOURCE]
    assert obs.created_items == []


def test_provision_places_an_existing_player(monkeypatch):
    obs = FakeObs()
    obs.scenes = list(ALL_SCENES)
    obs.inputs = [PLAYER_SOURCE, *OVERLAY_SOURCES]
    monkeypatch.setattr("app.obs.setup.obs_manager", obs)

    async def ensure_overlay():
        return None

    monkeypatch.setattr("app.obs.setup.ensure_overlay", ensure_overlay)
    result = asyncio.run(provision_obs())
    assert result["player"] == "colocada na cena"
    assert obs.created_inputs == []
    assert obs.created_items == [PLAYER_SOURCE]


def test_provision_reports_a_dead_socket(monkeypatch):
    class Dead:
        _ws = None

        async def connect(self):
            return False

    monkeypatch.setattr("app.obs.setup.obs_manager", Dead())
    result = asyncio.run(provision_obs())
    assert result == {"ok": False, "error": "OBS não está conectado."}


def test_cycle_does_not_create_a_missing_player(monkeypatch):
    created = []

    class Obs:
        async def get_scene_items(self, scene_name):
            return []

        async def create_input(self, *args, **kwargs):
            created.append(args)
            return {"sceneItemId": 1}

        async def set_input_settings(self, *args, **kwargs):
            return True

    monkeypatch.setattr("app.cycle.obs_manager", Obs())
    ok = asyncio.run(CycleManager()._ensure_player_source(r"C:\videos\queue\abc.mp4"))
    assert ok is False
    assert created == []
