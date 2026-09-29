"""Saved RTMP target. The .env values seed the first custom setup."""
from __future__ import annotations

from app.config import settings
from app.database import get_db
from app.obs.services import find_service, load_services, locate_services_file


async def get_stream_config() -> dict:
    async with get_db() as db:
        cursor = await db.execute("SELECT * FROM stream_config WHERE id = 1")
        row = await cursor.fetchone()
        if row is None:
            await db.execute(
                """INSERT INTO stream_config
                   (id, mode, service, region, stream_key, server_url, services_path)
                   VALUES (1, 'custom', '', '', ?, ?, '')""",
                (settings.RTMP_KEY, settings.RTMP_URL),
            )
            await db.commit()
            cursor = await db.execute("SELECT * FROM stream_config WHERE id = 1")
            row = await cursor.fetchone()
    return dict(row)


async def save_stream_config(
    *,
    mode: str,
    service: str,
    region: str,
    stream_key: str,
    server_url: str,
    services_path: str,
) -> None:
    current = await get_stream_config()
    key = stream_key.strip() or current["stream_key"]
    chosen = "preset" if mode == "preset" else "custom"
    async with get_db() as db:
        await db.execute(
            """UPDATE stream_config
               SET mode = ?, service = ?, region = ?, stream_key = ?, server_url = ?, services_path = ?
               WHERE id = 1""",
            (
                chosen,
                service.strip(),
                region.strip(),
                key,
                server_url.strip(),
                services_path.strip(),
            ),
        )
        await db.commit()


def service_payload(config: dict, services: list[dict]) -> tuple[str, dict] | str:
    """OBS SetStreamServiceSettings type and fields, or an error message."""
    key = (config.get("stream_key") or "").strip()
    if not key:
        return "Falta a chave de transmissão em Configs."
    if config.get("mode") != "preset":
        url = (config.get("server_url") or "").strip()
        if not url:
            return "Falta a URL do servidor em Configs."
        return "rtmp_custom", {"server": url, "key": key, "use_auth": False}

    service = find_service(services, config.get("service") or "")
    if service is None:
        return "O preset escolhido não está no services.json. Abra Configs."
    servers = service["servers"]
    region = (config.get("region") or "").strip()
    names = [item["name"] for item in servers]
    if len(servers) > 1:
        if region not in names:
            return "Escolha a região em Configs."
        server_name = region
    elif len(servers) == 1:
        server_name = names[0]
    else:
        server_name = ""
    payload = {"service": service["name"], "key": key}
    if server_name:
        payload["server"] = server_name
    return "rtmp_common", payload


def services_for(config: dict) -> tuple[list[dict], str]:
    """Presets and the path they came from. Empty path means nothing was found."""
    path = locate_services_file(config.get("services_path") or "")
    if path is None:
        return [], ""
    return load_services(path), str(path)
