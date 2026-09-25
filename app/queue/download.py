"""Download the front of the queue with yt-dlp and delete the rest."""
from __future__ import annotations

import asyncio
import logging
import os
import shutil
import subprocess
import time
from pathlib import Path

from app.config import settings
from app.database import get_db
from app.queue import cookies as yt_cookies

log = logging.getLogger(__name__)

# Probed once at startup, in this order. Args are passed to ffmpeg via yt-dlp.
_ENCODERS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("h264_nvenc", "NVIDIA NVENC H.264", ("-preset", "p4", "-rc", "vbr", "-cq", "23")),
    ("h264_amf", "AMD HW H.264", ("-quality", "balanced", "-rc", "cqp", "-qp_i", "23", "-qp_p", "23")),
    ("h264_qsv", "Intel Quick Sync H.264", ("-preset", "medium", "-global_quality", "23")),
    ("libx264", "CPU H.264", ("-preset", "veryfast", "-crf", "23")),
)

_encoder: str | None = None
_encoder_args: tuple[str, ...] = ()
_encoder_label: str = ""
_lock = asyncio.Lock()
_task: asyncio.Task | None = None
_dirty = False

_KEEP_STATUSES = ("playing", "pending")
_protected_ids: set[str] = set()


def protect_video_ids(video_ids: set[str]) -> None:
    """Video ids the live pass still needs. Sync will not delete their files."""
    global _protected_ids
    _protected_ids = set(video_ids)


def selected_encoder() -> str | None:
    return _encoder


def selected_encoder_label() -> str:
    return _encoder_label


def probe_encoders() -> str | None:
    """Pick the first H.264 encoder that can actually encode a short frame."""
    global _encoder, _encoder_args, _encoder_label
    for name, label, args in _ENCODERS:
        if _encoder_works(name, args):
            _encoder = name
            _encoder_args = args
            _encoder_label = label
            log.info("yt-dlp H.264 encoder: %s (%s)", label, name)
            return name
        log.info("H.264 encoder unavailable: %s (%s)", label, name)
    _encoder = None
    _encoder_args = ()
    _encoder_label = ""
    log.error("No working H.264 encoder for yt-dlp")
    return None


def _encoder_works(name: str, args: tuple[str, ...]) -> bool:
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", "testsrc=size=320x180:rate=30:duration=0.2",
        "-an", "-c:v", name, *args,
        "-f", "null", "-",
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.info("Encoder probe %s failed: %s", name, exc)
        return False
    if result.returncode != 0:
        err = (result.stderr or b"").decode("utf-8", "replace").strip()
        log.info("Encoder probe %s exit %s: %s", name, result.returncode, err[-300:])
        return False
    return True


def keep_ids(rows: list[dict], limit: int) -> list[str]:
    """Playing first, then the oldest pending. At most `limit` video ids."""
    if limit <= 0:
        return []
    eligible = [row for row in rows if row.get("status") in _KEEP_STATUSES]
    eligible.sort(key=lambda row: (
        0 if row.get("status") == "playing" else 1,
        int(row.get("published_unix") or 0),
        int(row.get("id") or 0),
    ))
    return [row["video_id"] for row in eligible[:limit]]


def files_outside_keep(directory: Path, keep: set[str]) -> list[Path]:
    """Files in directory whose video id is not in keep. Final MP4s of keep stay."""
    if not directory.is_dir():
        return []
    doomed: list[Path] = []
    for path in directory.iterdir():
        if not path.is_file():
            continue
        video_id = path.name.split(".", 1)[0]
        if video_id not in keep:
            doomed.append(path)
            continue
        # Leftover intermediates once the MP4 is in place.
        final = directory / f"{video_id}.mp4"
        if path != final and final.exists() and final.stat().st_size > 0:
            doomed.append(path)
    return doomed


def shutdown() -> None:
    """Cancel the sync task and stop ffmpeg/ffprobe it spawned."""
    if _task and not _task.done():
        _task.cancel()
    _kill_media_tools()


def _kill_media_tools() -> None:
    if os.name != "nt":
        return
    root = os.getpid()
    script = (
        "$all = Get-CimInstance Win32_Process; "
        f"$kids = @($all | Where-Object {{ $_.ParentProcessId -eq {root} }}); "
        "$ids = @($kids | ForEach-Object { $_.ProcessId }); "
        "$grand = @($all | Where-Object { $ids -contains $_.ParentProcessId }); "
        "@($kids + $grand) | Where-Object { $_.Name -in @('ffmpeg.exe','ffprobe.exe') } | "
        "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
    )
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-Command", script],
            capture_output=True, timeout=20,
        )
    except (OSError, subprocess.TimeoutExpired):
        log.warning("Could not stop child ffmpeg processes")


def schedule_sync() -> None:
    """Run a download sync soon. Overlapping calls share one follow-up pass."""
    global _dirty, _task
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    _dirty = True
    if _task is None or _task.done():
        _task = loop.create_task(_sync_loop())


async def _sync_loop() -> None:
    global _dirty
    async with _lock:
        while _dirty:
            _dirty = False
            try:
                await sync_downloads()
            except Exception:
                log.exception("Queue download sync failed")


async def purge_expired() -> int:
    """Delete expired rows and pending videos older than 24h, plus their files."""
    cutoff = int(time.time()) - 86400
    async with get_db() as db:
        cursor = await db.execute(
            """SELECT video_id, local_path FROM queue
               WHERE status = 'expired'
                  OR (status = 'pending' AND published_unix < ?)""",
            (cutoff,),
        )
        rows = [dict(row) for row in await cursor.fetchall()]
        await db.execute(
            """DELETE FROM queue
               WHERE status = 'expired'
                  OR (status = 'pending' AND published_unix < ?)""",
            (cutoff,),
        )
        await db.commit()
    for row in rows:
        _delete_paths(_leftovers(row["video_id"]))
        if row["local_path"]:
            _delete_paths([Path(row["local_path"])])
    if rows:
        log.info("Removed %d expired queue videos and their files", len(rows))
    return len(rows)


def discard_video_files(video_id: str, local_path: str = "") -> None:
    """Delete every file on disk that belongs to this video."""
    _delete_paths(_leftovers(video_id))
    if local_path:
        _delete_paths([Path(local_path)])


def _downloadable(row: dict) -> bool:
    if row.get("status") not in _KEEP_STATUSES:
        return False
    if row.get("status") == "pending" and int(row.get("published_unix") or 0) < int(time.time()) - 86400:
        return False
    return True


async def _one_row(video_id: str) -> dict | None:
    async with get_db() as db:
        cursor = await db.execute(
            "SELECT video_id, status, published_unix, local_path FROM queue WHERE video_id = ?",
            (video_id,),
        )
        row = await cursor.fetchone()
    return dict(row) if row else None


async def sync_downloads() -> None:
    """Download missing MP4s for the keep window and delete every other file."""
    settings.VIDEOS_DIR.mkdir(parents=True, exist_ok=True)
    await purge_expired()
    rows = await _queue_rows()
    keep = keep_ids(rows, settings.QUEUE_DOWNLOAD_KEEP)
    keep_set = set(keep) | set(_protected_ids)
    await _forget_paths_outside(keep_set)
    _delete_paths(files_outside_keep(settings.VIDEOS_DIR, keep_set))

    if not _encoder:
        log.warning("Skipping downloads: no H.264 encoder")
        return
    if not yt_cookies.ensure_fresh():
        log.warning("Skipping downloads: %s", yt_cookies.status()["message"])
        return

    by_id = {row["video_id"]: row for row in rows}
    for video_id in keep:
        final = settings.VIDEOS_DIR / f"{video_id}.mp4"
        current = await _one_row(video_id)
        if current is None or not _downloadable(current):
            _delete_paths(_leftovers(video_id))
            continue
        if final.exists() and final.stat().st_size > 0:
            await _store_path(video_id, final)
            continue
        if not _enough_free_space():
            log.warning(
                "Free space below %s MB; not downloading %s",
                settings.QUEUE_MIN_FREE_MB, video_id,
            )
            break
        try:
            await asyncio.to_thread(_download_mp4, video_id)
        except Exception as exc:
            log.exception("Download failed for %s", video_id)
            text = str(exc)
            if yt_cookies.looks_like_cookie_failure(text):
                yt_cookies.mark_rejected(
                    "O YouTube recusou os cookies durante o download. "
                    "Atualize o arquivo em cookies. Os vídeos não serão baixados."
                )
                await _store_error(video_id, "Cookies do YouTube recusados")
                _delete_paths(_leftovers(video_id))
                return
            await _store_error(video_id, text[:300])
            _delete_paths(_leftovers(video_id))
            continue
        current = await _one_row(video_id)
        if current is None or not _downloadable(current):
            _delete_paths(_leftovers(video_id))
            continue
        if final.exists() and final.stat().st_size > 0:
            await _store_path(video_id, final)
            _delete_paths([
                path for path in _leftovers(video_id) if path != final
            ])
        else:
            await _store_error(video_id, "yt-dlp did not produce an MP4")
        by_id.pop(video_id, None)


def _codecs(path: Path) -> tuple[str, str]:
    """Return (video codec, audio codec) from ffprobe. Empty string if missing."""
    cmd = [
        "ffprobe", "-v", "error",
        "-show_entries", "stream=codec_type,codec_name",
        "-of", "csv=p=0",
        str(path),
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.warning("ffprobe failed for %s: %s", path.name, exc)
        return "", ""
    video = audio = ""
    for line in (result.stdout or "").splitlines():
        parts = [part.strip() for part in line.split(",")]
        if len(parts) < 2:
            continue
        if "video" in parts and not video:
            video = next(part for part in parts if part != "video")
        elif "audio" in parts and not audio:
            audio = next(part for part in parts if part != "audio")
    return video, audio


def _enough_free_space() -> bool:
    try:
        free = shutil.disk_usage(settings.VIDEOS_DIR).free
    except OSError:
        return False
    return free >= settings.QUEUE_MIN_FREE_MB * 1024 * 1024


def _leftovers(video_id: str) -> list[Path]:
    directory = settings.VIDEOS_DIR
    if not directory.is_dir():
        return []
    return [
        path for path in directory.iterdir()
        if path.is_file() and (
            path.name == video_id or path.name.startswith(video_id + ".")
        )
    ]


def _delete_paths(paths: list[Path]) -> None:
    for path in paths:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            log.warning("Could not delete %s", path)


def _download_mp4(video_id: str) -> None:
    import yt_dlp
    from yt_dlp.postprocessor import FFmpegPostProcessor
    from yt_dlp.utils import PostProcessingError

    class _H264PP(FFmpegPostProcessor):
        """Remux H.264+AAC to MP4. Encode only the streams that are not already that."""

        def run(self, info):
            source = Path(info["filepath"])
            final = settings.VIDEOS_DIR / f"{video_id}.mp4"
            video_codec, audio_codec = _codecs(source)
            log.info(
                "Downloaded %s video=%s audio=%s container=%s",
                video_id, video_codec or "?", audio_codec or "?", source.suffix,
            )
            if video_codec == "h264" and audio_codec == "aac" and source.suffix.lower() == ".mp4":
                log.info("Already H.264 + AAC in MP4, no encode: %s", video_id)
                if source.resolve() != final.resolve():
                    source.replace(final)
                info["filepath"] = str(final)
                info["ext"] = "mp4"
                return [], info
            if video_codec in {"", "h264"} and audio_codec in {"", "aac"}:
                log.info("Remux only: %s", video_id)
                args = ["-c", "copy"]
            elif video_codec == "h264":
                log.info("Copy video, encode audio to AAC: %s", video_id)
                args = ["-c:v", "copy", "-c:a", "aac", "-b:a", "160k"]
            else:
                if not _encoder:
                    raise PostProcessingError("no H.264 encoder for a non-H.264 video")
                log.info("Encode video to H.264 because codec is %s: %s", video_codec, video_id)
                args = ["-c:v", _encoder, *_encoder_args, "-c:a", "aac", "-b:a", "160k"]
            temp = settings.VIDEOS_DIR / f"{video_id}.encode.mp4"
            if temp.exists():
                temp.unlink()
            self.run_ffmpeg(str(source), str(temp), args)
            if not temp.exists() or temp.stat().st_size <= 0:
                raise PostProcessingError("ffmpeg produced no MP4")
            temp.replace(final)
            info["filepath"] = str(final)
            info["ext"] = "mp4"
            if source.resolve() == final.resolve():
                return [], info
            return [str(source)], info

    outtmpl = str(settings.VIDEOS_DIR / "%(id)s.%(ext)s")
    opts = {
        "outtmpl": outtmpl,
        "format": (
            "bv*[vcodec^=avc1][height<=1080]+ba[acodec^=mp4a]/"
            "bv*[vcodec^=avc1][height<=1080]+ba/"
            "b[vcodec^=avc1][height<=1080]/"
            "bv*[height<=1080]+ba/b[height<=1080]/bv*+ba/b"
        ),
        "merge_output_format": "mp4",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "retries": 3,
        "fragment_retries": 3,
        "windowsfilenames": True,
    }
    cookiefile = yt_cookies.netscape_path()
    if cookiefile is None:
        raise RuntimeError("YouTube cookies are not valid")
    opts["cookiefile"] = str(cookiefile)
    url = f"https://www.youtube.com/watch?v={video_id}"
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.add_post_processor(_H264PP(ydl), when="post_process")
        ydl.download([url])


async def _queue_rows() -> list[dict]:
    async with get_db() as db:
        cursor = await db.execute(
            "SELECT id, video_id, status, published_unix, local_path FROM queue"
        )
        return [dict(row) for row in await cursor.fetchall()]


async def _forget_paths_outside(keep: set[str]) -> None:
    async with get_db() as db:
        cursor = await db.execute(
            "SELECT video_id, local_path FROM queue WHERE local_path != '' OR download_error != ''"
        )
        rows = await cursor.fetchall()
        for row in rows:
            if row["video_id"] in keep:
                continue
            path = Path(row["local_path"]) if row["local_path"] else None
            if path is not None:
                _delete_paths([path])
            await db.execute(
                "UPDATE queue SET local_path = '', download_error = '' WHERE video_id = ?",
                (row["video_id"],),
            )
        await db.commit()


async def _store_path(video_id: str, path: Path) -> None:
    async with get_db() as db:
        await db.execute(
            "UPDATE queue SET local_path = ?, download_error = '' WHERE video_id = ?",
            (str(path.resolve()), video_id),
        )
        await db.commit()


async def _store_error(video_id: str, message: str) -> None:
    async with get_db() as db:
        await db.execute(
            "UPDATE queue SET local_path = '', download_error = ? WHERE video_id = ?",
            (message, video_id),
        )
        await db.commit()
