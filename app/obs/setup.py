"""Create the canonical OBS scenes and the sources the cycle expects.

Nothing here runs on connect. Configurações calls provision_obs.
"""
from __future__ import annotations

from app.obs.manager import ALL_SCENES, SCENE_PROGRAM, obs_manager
from app.overlay.service import OVERLAY_SOURCES, ensure_overlay

PLAYER_SOURCE = "NewsTube Fila"


async def provision_obs() -> dict:
    """Create missing scenes, NewsTube Fila, and the lower third. Leave existing items."""
    if not getattr(obs_manager, "_ws", None):
        if not await obs_manager.connect():
            return {"ok": False, "error": "OBS não está conectado."}

    before = {item.get("sceneName") for item in await obs_manager.get_scene_list()}
    await obs_manager._ensure_scenes()
    after = {item.get("sceneName") for item in await obs_manager.get_scene_list()}
    created_scenes = [name for name in ALL_SCENES if name not in before and name in after]

    inputs = {item.get("inputName") for item in await obs_manager.get_input_list()}
    placed = {
        item.get("sourceName")
        for item in await obs_manager.get_scene_items(SCENE_PROGRAM)
    }
    player = ""
    if PLAYER_SOURCE not in inputs:
        created = await obs_manager.create_input(
            PLAYER_SOURCE,
            "ffmpeg_source",
            {"is_local_file": True, "local_file": "", "looping": False},
            scene_name=SCENE_PROGRAM,
        )
        if created is not None:
            player = "criada"
    elif PLAYER_SOURCE not in placed:
        added = await obs_manager.create_scene_item(SCENE_PROGRAM, PLAYER_SOURCE)
        if added is not None:
            player = "colocada na cena"

    inputs_before = set(inputs)
    if player == "criada":
        inputs_before.add(PLAYER_SOURCE)
    await ensure_overlay()
    inputs_after = {item.get("inputName") for item in await obs_manager.get_input_list()}
    created_overlay = [name for name in OVERLAY_SOURCES if name not in inputs_before and name in inputs_after]

    parts = []
    if created_scenes:
        parts.append("cenas: " + ", ".join(created_scenes))
    if player:
        parts.append(f"NewsTube Fila {player}")
    if created_overlay:
        parts.append("lower third: " + ", ".join(created_overlay))
    message = "Criado — " + "; ".join(parts) + "." if parts else "Nada a criar. As cenas e as fontes já existem."
    return {
        "ok": True,
        "message": message,
        "created_scenes": created_scenes,
        "player": player,
        "created_overlay": created_overlay,
    }
