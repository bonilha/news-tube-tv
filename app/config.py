from pathlib import Path
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    ADMIN_USER: str = "admin"
    ADMIN_PASSWORD: str = "changeme"

    # OBS WebSocket connection (obs-websocket v5, built-in since OBS 28)
    OBS_WS_URL: str = "ws://localhost:4455"
    OBS_WS_PASSWORD: str = ""

    # Video settings (used when creating scenes via WebSocket)
    OBS_BASE_WIDTH: int = 1920
    OBS_BASE_HEIGHT: int = 1080
    OBS_FPS_NUM: int = 30000
    OBS_FPS_DEN: int = 1001

    # Empty uses the OBS install location for the platform.
    OBS_SERVICES_JSON: str = ""
    RTMP_URL: str = "rtmp://localhost/live"
    RTMP_KEY: str = "stream"

    # Address OBS uses to open the QR browser source. Another PC needs this machine, not 127.0.0.1.
    OVERLAY_BASE_URL: str = "http://127.0.0.1:8000"

    ASSETS_DIR: Path = Path("assets")
    DB_PATH: Path = Path("data.db")

    # Invidious instance for YouTube data
    INVIDIOUS_URL: str = "http://192.168.66.170:3000"

    # Language sent to Invidious (hl). Titles use this when a translation exists.
    CONTENT_LANGUAGE: str = "pt-BR"

    # How often to scan channels and expire queued videos. 0 disables the loop.
    QUEUE_REFRESH_MINUTES: int = 15

    # How often to top up the on-disk MP4 buffer. 0 disables only this loop.
    QUEUE_BUFFER_MINUTES: int = 5

    # Browser cookie export used by yt-dlp. Downloads do not run unless it passes.
    COOKIES_DIR: Path = Path("cookies")

    # videos/downloads holds the yt-dlp files. videos/queue holds playable MP4s.
    VIDEOS_DIR: Path = Path("videos")
    QUEUE_DOWNLOAD_KEEP: int = 5
    # Maximum video duration in minutes to be eligible for the queue (default 30 min)
    QUEUE_MAX_DURATION_MINUTES: int = 30
    # Give up on a media that will not finish and re-arm the cycle. 0 waits forever.
    CYCLE_MAX_WAIT_MINUTES: int = 90
    # Stop starting a new download when the volume has less than this free.
    QUEUE_MIN_FREE_MB: int = 1024

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}


settings = Settings()
