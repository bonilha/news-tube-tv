"""OBS RTMP service list from the installed rtmp-services plugin."""
from __future__ import annotations

import json
import sys
from pathlib import Path

from app.config import settings


def candidate_paths() -> list[Path]:
    """Install locations for services.json, per operating system."""
    home = Path.home()
    if sys.platform == "win32":
        roots = [
            os.environ.get("ProgramFiles", r"C:\Program Files"),
            os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
        ]
        return [
            Path(root) / "obs-studio" / "data" / "obs-plugins" / "rtmp-services" / "services.json"
            for root in roots
            if root
        ]
    if sys.platform == "darwin":
        return [
            Path("/Applications/OBS.app/Contents/Resources/data/obs-plugins/rtmp-services/services.json"),
            home / "Applications/OBS.app/Contents/Resources/data/obs-plugins/rtmp-services/services.json",
        ]
    flatpak = (
        home
        / ".local/share/flatpak/app/com.obsproject.Studio/current/active/files"
        / "share/obs/obs-plugins/rtmp-services/services.json"
    )
    return [
        Path("/usr/share/obs/obs-plugins/rtmp-services/services.json"),
        Path("/usr/local/share/obs/obs-plugins/rtmp-services/services.json"),
        Path("/snap/obs-studio/current/usr/share/obs/obs-plugins/rtmp-services/services.json"),
        flatpak,
    ]


def locate_services_file(saved_path: str = "") -> Path | None:
    """Saved path wins. Otherwise the environment, then the install locations."""
    ordered: list[Path] = []
    if saved_path.strip():
        ordered.append(Path(saved_path.strip()))
    env = (settings.OBS_SERVICES_JSON or "").strip()
    if env:
        ordered.append(Path(env))
    if not saved_path.strip():
        ordered.extend(candidate_paths())
    for path in ordered:
        if path.is_file():
            return path
    return None


def load_services(path: Path | None) -> list[dict]:
    """Presets as name, alt names, and server name/url pairs."""
    if path is None:
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    raw = data.get("services") if isinstance(data, dict) else data
    if not isinstance(raw, list):
        return []
    services: list[dict] = []
    for item in raw:
        if not isinstance(item, dict) or not item.get("name"):
            continue
        servers = []
        for server in item.get("servers") or []:
            if isinstance(server, dict) and server.get("name"):
                servers.append({
                    "name": str(server["name"]),
                    "url": str(server.get("url") or ""),
                })
        alt = [str(name) for name in (item.get("alt_names") or []) if name]
        services.append({
            "name": str(item["name"]),
            "alt_names": alt,
            "servers": servers,
        })
    services.sort(key=lambda item: item["name"].lower())
    return services


def find_service(services: list[dict], name: str) -> dict | None:
    wanted = name.strip().lower()
    if not wanted:
        return None
    for service in services:
        names = [service["name"], *service["alt_names"]]
        if any(item.lower() == wanted for item in names):
            return service
    return None
