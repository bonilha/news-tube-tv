from __future__ import annotations

import shutil
import uuid
from pathlib import Path

from fastapi import UploadFile

from app.config import settings
from app.database import insert_asset, list_assets, delete_asset, get_asset

MAX_UPLOAD_BYTES = 100 * 1024 * 1024


class UploadRejected(Exception):
    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


def _mime_allowed(content_type: str | None) -> bool:
    mime = (content_type or "").split(";", 1)[0].strip().lower()
    return mime.startswith("image/") or mime in {"video/mp4", "video/webm"}


class _Capped:
    """Stop the copy once the running total passes the limit."""

    def __init__(self, raw, limit: int):
        self.raw = raw
        self.limit = limit
        self.size = 0

    def read(self, n: int = -1) -> bytes:
        if n is None or n < 0:
            n = 1024 * 1024
        data = self.raw.read(n)
        self.size += len(data)
        if self.size > self.limit:
            raise UploadRejected("Arquivo maior que 100 MB.")
        return data


class AssetService:
    async def upload_file(self, file: UploadFile) -> dict:
        if not _mime_allowed(file.content_type):
            raise UploadRejected("Tipo não aceito. Use imagem, MP4 ou WebM.")

        ext = Path(file.filename or "file").suffix
        unique_name = f"{uuid.uuid4()}{ext}"

        dest = settings.ASSETS_DIR / unique_name
        partial = dest.with_name(dest.name + ".part")
        settings.ASSETS_DIR.mkdir(parents=True, exist_ok=True)

        capped = _Capped(file.file, MAX_UPLOAD_BYTES)
        try:
            with partial.open("wb") as out:
                shutil.copyfileobj(capped, out)
        except UploadRejected:
            partial.unlink(missing_ok=True)
            raise
        partial.replace(dest)
        size = capped.size

        asset_id = await insert_asset(
            filename=unique_name,
            original_name=file.filename or "unknown",
            mime_type=file.content_type or "application/octet-stream",
            size=size,
        )
        return await get_asset(asset_id)

    async def list_files(self) -> list[dict]:
        return await list_assets()

    async def delete_file(self, asset_id: int) -> bool:
        asset = await get_asset(asset_id)
        if not asset:
            return False
        file_path = settings.ASSETS_DIR / asset["filename"]
        if file_path.exists():
            file_path.unlink()
        await delete_asset(asset_id)
        return True

    async def get_file_path(self, asset_id: int) -> Path | None:
        asset = await get_asset(asset_id)
        if not asset:
            return None
        path = settings.ASSETS_DIR / asset["filename"]
        return path if path.exists() else None


asset_service = AssetService()
