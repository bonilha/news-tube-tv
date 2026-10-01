"""Download the front of the queue with yt-dlp and delete the rest."""
from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

from app.config import settings
from app.database import get_db
from app.queue import cookies as yt_cookies

log = logging.getLogger(__name__)

# A YouTube video id. Anything else on disk is not ours to delete.
_VIDEO_ID = re.compile(r"[A-Za-z0-9_-]{11}")

# Probed once at startup, in this order. Each encoder can try more than one arg set.
_ENCODERS: tuple[tuple[str, str, tuple[tuple[str, ...], ...]], ...] = (
    ("h264_nvenc", "NVIDIA NVENC H.264", (
        ("-preset", "p4", "-rc", "vbr", "-cq", "23"),
        ("-preset", "fast", "-rc", "vbr", "-cq", "23"),
    )),
    ("h264_amf", "AMD HW H.264", (
        ("-quality", "balanced", "-rc", "cqp", "-qp_i", "23", "-qp_p", "23"),
    )),
    ("h264_qsv", "Intel Quick Sync H.264", (
        ("-preset", "medium", "-global_quality", "23"),
    )),
    ("libx264", "CPU H.264", (
        ("-preset", "veryfast", "-crf", "23"),
    )),
)
# A stalled YouTube connection must not hold the download lock until the process dies.
_SOCKET_TIMEOUT_SEC = 30
_LATE = "Não ficou pronto no prazo da fila"


def downloads_dir() -> Path:
    """Raw yt-dlp files. They are not playable and do not count as queued."""
    return settings.VIDEOS_DIR / "downloads"


def queue_dir() -> Path:
    """Processed MP4s ready to play or already playing."""
    return settings.VIDEOS_DIR / "queue"


def queue_file(video_id: str) -> Path:
    return queue_dir() / f"{video_id}.mp4"


def ensure_video_dirs() -> None:
    downloads_dir().mkdir(parents=True, exist_ok=True)
    queue_dir().mkdir(parents=True, exist_ok=True)

_encoder: str | None = None
_encoder_args: tuple[str, ...] = ()
_encoder_label: str = ""
_lock = asyncio.Lock()
_task: asyncio.Task | None = None
_dirty = False
# Closed while startup or "Apagar fila e escanear" cleans, scans, and shuffles.
_download_hold = 0
# Id inside yt-dlp. A new hold abandons it so the next id does not start.
_current_download = ""

_KEEP_STATUSES = ("playing", "pending")
_protected_ids: set[str] = set()
_hold_player_release = False
# Already shown this pass. They do not take a slot ahead of videos still waiting.
_skip_keep: set[str] = set()
# File OBS is playing. Kept on disk and not counted inside the five.
_on_air_id: str = ""
_stuck_deletes: set[str] = set()
# Ids whose deadline already fired. The worker must not publish a late file.
_abandoned: set[str] = set()
_PLAYER_SOURCE = "NewsTube Fila"
_MEDIA_STOP = "OBS_WEBSOCKET_MEDIA_INPUT_ACTION_STOP"


def protect_video_ids(video_ids: set[str]) -> None:
    """Video ids the live pass still needs. Sync will not delete their files."""
    global _protected_ids
    _protected_ids = set(video_ids)


def hold_player_release(hold: bool) -> None:
    """While the cycle waits on the bumper, do not stop NewsTube Fila."""
    global _hold_player_release
    _hold_player_release = hold


def skip_keep_ids(video_ids: set[str]) -> None:
    """Ids already shown this pass. They fill a slot only after the unplayed run out."""
    global _skip_keep
    _skip_keep = set(video_ids)


def note_on_air(video_id: str) -> None:
    """The file OBS is playing stays on disk and does not use a window slot."""
    global _on_air_id
    _on_air_id = video_id or ""


def ready_wait_seconds(rows: list[dict], on_air_id: str = "", is_ready=None) -> float:
    """Half the duration of MP4s that are ready and not on air.

    `duration` is seconds. The video on air does not count. A row without a
    file does not count. Zero means there is nothing downloaded to spend
    waiting, so the caller must not arm a zero-second download cap.
    """
    if is_ready is None:
        def is_ready(row):
            path = row.get("local_path") or ""
            return bool(path) and Path(path).is_file()
    total = 0
    for row in rows:
        video_id = row.get("video_id") or ""
        if not video_id or video_id == (on_air_id or ""):
            continue
        if not is_ready(row):
            continue
        total += max(0, int(row.get("duration") or 0))
    return total / 2


def selected_encoder() -> str | None:
    return _encoder


def selected_encoder_label() -> str:
    return _encoder_label


def probe_encoders() -> str | None:
    """Pick the first H.264 encoder that can actually encode a short frame."""
    global _encoder, _encoder_args, _encoder_label
    for name, label, arg_sets in _ENCODERS:
        for args in arg_sets:
            if _encoder_works(name, args):
                _encoder = name
                _encoder_args = args
                _encoder_label = label
                log.info("yt-dlp H.264 encoder: %s (%s) %s", label, name, " ".join(args))
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


def keep_ids(rows: list[dict], limit: int, skip: set[str] | None = None) -> list[str]:
    """Next `limit` video ids that are still unplayed this pass.

    After a shuffle, play_order is the queue. Before that, playing comes
    first and then the oldest pending. Ids in `skip` (already aired this
    pass) do not consume a slot. Videos with a download_error are also
    excluded — they cannot be downloaded until the user retries them manually.
    """
    if limit <= 0:
        return []
    skipped = skip or set()
    eligible = [
        row for row in rows
        if row.get("status") in _KEEP_STATUSES
        and row.get("video_id") not in skipped
        and not download_blocked(row)
    ]
    shuffled = any(int(row.get("play_order") or 0) > 0 for row in rows)
    if shuffled:
        eligible.sort(key=lambda row: (
            int(row.get("play_order") or 0),
            int(row.get("id") or 0),
        ))
    else:
        eligible.sort(key=lambda row: (
            0 if row.get("status") == "playing" else 1,
            int(row.get("published_unix") or 0),
            int(row.get("id") or 0),
        ))
    return [row["video_id"] for row in eligible[:limit]]


def buffer_ids(rows: list[dict], limit: int, skip: set[str] | None = None) -> list[str]:
    """Unplayed ids first. Aired ids fill only the slots those leave empty.

    Five videos still waiting this pass use the whole window, so nothing from
    the next lap is downloaded yet. With four waiting, the first aired id in
    play order takes the free slot. Download errors stay out of the window.
    """
    fresh = keep_ids(rows, limit, skip)
    if len(fresh) >= limit:
        return fresh
    remaining_limit = limit - len(fresh)
    seen = set(fresh)
    skipped = skip or set()
    filler: list[str] = []
    shuffled = any(int(row.get("play_order") or 0) > 0 for row in rows)
    candidates = [
        row for row in rows
        if row.get("video_id") in skipped
        and row.get("video_id") not in seen
        and not download_blocked(row)
    ]
    if shuffled:
        candidates.sort(key=lambda r: (int(r.get("play_order") or 0), int(r.get("id") or 0)))
    else:
        candidates.sort(key=lambda r: (int(r.get("published_unix") or 0), int(r.get("id") or 0)))
    for r in candidates:
        if len(filler) >= remaining_limit:
            break
        vid = r.get("video_id")
        if vid and vid not in seen:
            filler.append(vid)
            seen.add(vid)
    return fresh + filler


def retain_ids(rows: list[dict], limit: int, skip: set[str] | None = None, on_air: str = "") -> set[str]:
    """Files that stay: the download window, plus the id on air."""
    keep = set(buffer_ids(rows, limit, skip))
    if on_air:
        keep.add(on_air)
    return keep


def files_outside_keep(directory: Path, keep: set[str]) -> list[Path]:
    """Files in directory whose video id is not in keep. Final MP4s of keep stay."""
    if not directory.is_dir():
        return []
    doomed: list[Path] = []
    for path in directory.iterdir():
        if not path.is_file():
            continue
        video_id = path.name.split(".", 1)[0]
        if not _VIDEO_ID.fullmatch(video_id):
            # A bumper, an art file, anything the queue never put here.
            continue
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


def downloads_held() -> bool:
    """True while the queue is still being cleaned, scanned, or shuffled."""
    return _download_hold > 0


def hold_downloads() -> None:
    """Stop the five-video window until release_downloads()."""
    global _download_hold
    _download_hold += 1
    if _current_download:
        _abandoned.add(_current_download)
        _kill_media_tools()


def release_downloads() -> None:
    """Open the window. The last release is the one that may download."""
    global _download_hold
    if _download_hold <= 0:
        _download_hold = 0
        return
    _download_hold -= 1
    if _download_hold == 0:
        schedule_sync()


def schedule_sync() -> None:
    """Run a download sync soon. Overlapping calls share one follow-up pass.

    While the queue is being scanned and shuffled this only remembers the
    request. release_downloads() starts it after the new order exists.
    """
    global _dirty, _task
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    _dirty = True
    if downloads_held():
        return
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
    """Delete expired rows, pending videos older than 24h, and videos exceeding max duration, plus files.

    Ids still needed by the live pass stay in the queue until that pass ends.
    """
    cutoff = int(time.time()) - 86400
    max_duration_sec = settings.QUEUE_MAX_DURATION_MINUTES * 60 if settings.QUEUE_MAX_DURATION_MINUTES > 0 else 999999999
    protected = set(_protected_ids)
    async with get_db() as db:
        cursor = await db.execute(
            """SELECT video_id, local_path FROM queue
               WHERE status = 'expired'
                  OR (status = 'pending' AND published_unix < ?)
                  OR (status != 'playing' AND duration > ?)""",
            (cutoff, max_duration_sec),
        )
        rows = [
            dict(row) for row in await cursor.fetchall()
            if row["video_id"] not in protected
        ]
        if rows:
            placeholders = ",".join("?" * len(rows))
            await db.execute(
                f"""DELETE FROM queue
                    WHERE video_id IN ({placeholders})
                      AND (
                        status = 'expired'
                        OR (status = 'pending' AND published_unix < ?)
                        OR (status != 'playing' AND duration > ?)
                      )""",
                [row["video_id"] for row in rows] + [cutoff, max_duration_sec],
            )
            await db.commit()
    doomed: list[Path] = []
    for row in rows:
        doomed.extend(_leftovers(row["video_id"]))
        if row["local_path"]:
            doomed.append(Path(row["local_path"]))
    await remove_files(doomed)
    if rows:
        log.info("Removed %d expired queue videos and their files", len(rows))
    return len(rows)


async def discard_video_files(video_id: str, local_path: str = "") -> None:
    """Delete every file on disk that belongs to this video."""
    paths = _leftovers(video_id)
    if local_path:
        paths.append(Path(local_path))
    await remove_files(paths)


def download_blocked(row: dict) -> bool:
    """Too old, too long, failed, or not a queue status. It must not take a window slot."""
    if row.get("status") not in ("playing", "pending", "played"):
        return True
    if row.get("download_error"):
        return True
    if int(row.get("published_unix") or 0) < int(time.time()) - 86400:
        return True
    max_duration_sec = settings.QUEUE_MAX_DURATION_MINUTES * 60 if settings.QUEUE_MAX_DURATION_MINUTES > 0 else 999999999
    if int(row.get("duration") or 0) > max_duration_sec:
        return True
    return False


def _downloadable(row: dict) -> bool:
    return not download_blocked(row)


async def _one_row(video_id: str) -> dict | None:
    async with get_db() as db:
        cursor = await db.execute(
            "SELECT video_id, status, published_unix, local_path, duration FROM queue WHERE video_id = ?",
            (video_id,),
        )
        row = await cursor.fetchone()
    return dict(row) if row else None


def needs_video_scale(video_codec: str, width: int, height: int, target: tuple[int, int]) -> bool:
    """H.264 is remuxed. OBS fits it to the canvas. Other codecs are scaled while encoded."""
    if video_codec == "h264":
        return False
    return width > 0 and height > 0 and (width, height) != target


async def sync_downloads() -> None:
    """Download missing MP4s for the keep window and delete every other file."""
    if downloads_held():
        return
    ensure_video_dirs()
    await purge_expired()
    if downloads_held():
        return
    rows = await _queue_rows()
    await _clear_missing_paths(rows)
    await _clear_finishable_errors(rows)
    keep = buffer_ids(rows, settings.QUEUE_DOWNLOAD_KEEP, _skip_keep)
    keep_set = retain_ids(rows, settings.QUEUE_DOWNLOAD_KEEP, _skip_keep, _on_air_id)

    if not _encoder:
        log.warning("Skipping downloads: no H.264 encoder")
    elif not yt_cookies.ensure_fresh():
        log.warning("Skipping downloads: %s", yt_cookies.status()["message"])
    else:
        await _download_keep(keep)

    if downloads_held():
        return
    await _forget_paths_outside(keep_set)
    doomed: list[Path] = []
    for directory in (downloads_dir(), queue_dir(), settings.VIDEOS_DIR):
        doomed.extend(files_outside_keep(directory, keep_set))
    await remove_files(doomed)


async def _download_keep(keep: list[str]) -> None:
    """Download MP4s for the window. Missing files do not keep other ids on disk."""
    global _current_download
    for video_id in keep:
        if downloads_held():
            return
        final = queue_file(video_id)
        legacy = settings.VIDEOS_DIR / f"{video_id}.mp4"
        if not final.exists() and legacy.is_file():
            legacy.replace(final)
        current = await _one_row(video_id)
        if current is None or not _downloadable(current):
            await remove_files(_leftovers(video_id))
            continue
        if final.exists() and final.stat().st_size > 0:
            await _store_path(video_id, final)
            continue
        if raw_download(video_id):
            try:
                await asyncio.to_thread(_place_in_queue, video_id)
            except Exception as exc:
                log.exception("Could not move %s into the queue folder", video_id)
                await _store_error(video_id, str(exc)[:300])
                continue
            if final.exists() and final.stat().st_size > 0:
                await _store_path(video_id, final)
                await remove_files([
                    path for path in _leftovers(video_id) if path != final
                ])
                continue
        if not _enough_free_space():
            log.warning(
                "Free space below %s MB; not downloading %s",
                settings.QUEUE_MIN_FREE_MB, video_id,
            )
            break
        budget = ready_wait_seconds(await _queue_rows(), _on_air_id)
        _current_download = video_id
        try:
            await _run_download(video_id, budget)
        except TimeoutError:
            log.error("Download missed the queue deadline for %s", video_id)
            _abandoned.add(video_id)
            _kill_media_tools()
            await _store_error(video_id, _LATE)
            await remove_files(_leftovers(video_id))
            continue
        except Exception as exc:
            if downloads_held() or video_id in _abandoned:
                log.info("Download stopped until the queue is shuffled: %s", video_id)
                return
            log.exception("Download failed for %s", video_id)
            text = str(exc)
            if yt_cookies.looks_like_cookie_failure(text):
                # One video's 403 is not proof the cookies are dead. Age gates,
                # private videos and geo blocks answer the same way. Re-probe on
                # the next sync and only check_cookies may reject them outright.
                yt_cookies.invalidate()
            await _store_error(video_id, text[:300])
            await remove_files(_leftovers(video_id))
            continue
        finally:
            if _current_download == video_id:
                _current_download = ""
        current = await _one_row(video_id)
        if current is None or not _downloadable(current):
            await remove_files(_leftovers(video_id))
            continue
        if final.exists() and final.stat().st_size > 0:
            await _store_path(video_id, final)
            await remove_files([
                path for path in _leftovers(video_id) if path != final
            ])
        else:
            await _store_error(video_id, "yt-dlp did not produce an MP4")


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


def _video_size(path: Path) -> tuple[int, int]:
    """Pixel size of the first video stream. (0, 0) when ffprobe cannot tell."""
    cmd = [
        "ffprobe", "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=width,height",
        "-of", "csv=p=0:s=x",
        str(path),
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.warning("ffprobe size failed for %s: %s", path.name, exc)
        return 0, 0
    line = (result.stdout or "").strip().splitlines()
    if not line or "x" not in line[0]:
        return 0, 0
    width_text, _, height_text = line[0].partition("x")
    try:
        return int(width_text), int(height_text)
    except ValueError:
        return 0, 0


def _scale_filter() -> str:
    """Fit inside the OBS canvas and pad so the file is exactly that size."""
    width = settings.OBS_BASE_WIDTH
    height = settings.OBS_BASE_HEIGHT
    return (
        f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:black,setsar=1"
    )


def _enough_free_space() -> bool:
    try:
        free = shutil.disk_usage(settings.VIDEOS_DIR).free
    except OSError:
        return False
    return free >= settings.QUEUE_MIN_FREE_MB * 1024 * 1024


def _leftovers(video_id: str) -> list[Path]:
    found: list[Path] = []
    for directory in (downloads_dir(), queue_dir(), settings.VIDEOS_DIR):
        if not directory.is_dir():
            continue
        found.extend(
            path for path in directory.iterdir()
            if path.is_file() and (
                path.name == video_id or path.name.startswith(video_id + ".")
            )
        )
    return found


def _same_file(left: Path, right: Path) -> bool:
    try:
        return left.resolve() == right.resolve()
    except OSError:
        return os.path.normcase(str(left)) == os.path.normcase(str(right))


async def _release_obs_file(paths: list[Path]) -> None:
    """Stop NewsTube Fila when it still has one of these files open."""
    from app.obs.manager import obs_manager

    if _hold_player_release:
        return
    info = await obs_manager.get_input_settings(_PLAYER_SOURCE)
    local = str((info.get("inputSettings") or {}).get("local_file") or "")
    if not local:
        return
    current = Path(local)
    if not any(_same_file(current, path) for path in paths):
        return
    if current.name.split(".", 1)[0] in _protected_ids:
        return
    await obs_manager.trigger_media(_PLAYER_SOURCE, _MEDIA_STOP)
    await obs_manager.set_input_settings(_PLAYER_SOURCE, {
        "is_local_file": True,
        "local_file": "",
        "close_when_inactive": True,
        "looping": False,
    })
    await asyncio.sleep(0.4)


async def remove_files(paths: list[Path]) -> None:
    """Delete files. If OBS still holds one, close that source and retry once."""
    unique: list[Path] = []
    seen: set[str] = set()
    for path in paths:
        key = os.path.normcase(str(path))
        if key in seen:
            continue
        seen.add(key)
        unique.append(path)
    if not unique:
        return
    failed = [path for path in unique if not _try_unlink(path)]
    if not failed:
        return
    await _release_obs_file(failed)
    for path in failed:
        _try_unlink(path, locked_message=True)


def _try_unlink(path: Path, *, locked_message: bool = False) -> bool:
    key = os.path.normcase(str(path))
    try:
        path.unlink(missing_ok=True)
    except OSError:
        if locked_message and key not in _stuck_deletes:
            log.warning("Arquivo em uso, nova tentativa no próximo ciclo: %s", path)
            _stuck_deletes.add(key)
        return False
    _stuck_deletes.discard(key)
    return True


# yt-dlp names a merged file after the video id. Fragments carry .f<itag>. before it.
_MERGED_SUFFIXES = (".mp4", ".webm", ".mkv", ".m4v", ".mov")


def raw_download(video_id: str) -> Path | None:
    """Finished yt-dlp file for this id. Partial fragments do not count."""
    directory = downloads_dir()
    if not directory.is_dir():
        return None
    finished = [
        path for path in directory.iterdir()
        if path.is_file()
        and path.stat().st_size > 0
        and path.name.startswith(video_id + ".")
        and ".part" not in path.name
        and ".encode." not in path.name
    ]
    by_name = {path.name: path for path in finished}
    for suffix in _MERGED_SUFFIXES:
        path = by_name.get(f"{video_id}{suffix}")
        if path is not None:
            return path
    return None


def _run_ffmpeg(source: Path, dest: Path, args: list[str]) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-i", str(source), *args, str(dest),
    ]
    try:
        result = subprocess.run(cmd, capture_output=True)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"ffmpeg failed: {exc}") from exc
    if result.returncode != 0 or not dest.exists() or dest.stat().st_size <= 0:
        err = (result.stderr or b"").decode("utf-8", "replace").strip()
        dest.unlink(missing_ok=True)
        raise RuntimeError(err[-400:] or "ffmpeg produced no MP4")


def _place_in_queue(video_id: str) -> None:
    """Move or encode the downloaded file into videos/queue. Runs after yt-dlp."""
    if video_id in _abandoned:
        return
    source = raw_download(video_id)
    if source is None:
        raise RuntimeError("yt-dlp did not leave a finished file in downloads")
    final = queue_file(video_id)
    video_codec, audio_codec = _codecs(source)
    width, height = _video_size(source)
    target = (settings.OBS_BASE_WIDTH, settings.OBS_BASE_HEIGHT)
    needs_scale = needs_video_scale(video_codec, width, height, target)
    log.info(
        "Downloaded %s video=%s audio=%s container=%s size=%sx%s",
        video_id, video_codec or "?", audio_codec or "?", source.suffix,
        width or "?", height or "?",
    )
    if (
        not needs_scale
        and video_codec == "h264"
        and audio_codec == "aac"
        and source.suffix.lower() == ".mp4"
    ):
        log.info("Already H.264 + AAC in MP4, moving to queue: %s", video_id)
        if source.resolve() != final.resolve():
            final.parent.mkdir(parents=True, exist_ok=True)
            source.replace(final)
        return
    if needs_scale:
        if not _encoder:
            raise RuntimeError("no H.264 encoder to scale to Full HD")
        log.info("Scale %sx%s to %sx%s: %s", width, height, target[0], target[1], video_id)
        audio = ["-c:a", "copy"] if audio_codec == "aac" else ["-c:a", "aac", "-b:a", "160k"]
        args = ["-vf", _scale_filter(), "-c:v", _encoder, *_encoder_args, *audio]
    elif video_codec in {"", "h264"} and audio_codec in {"", "aac"}:
        log.info("Remux only: %s", video_id)
        args = ["-c", "copy"]
    elif video_codec == "h264":
        log.info("Copy video, encode audio to AAC: %s", video_id)
        args = ["-c:v", "copy", "-c:a", "aac", "-b:a", "160k"]
    else:
        if not _encoder:
            raise RuntimeError(f"no H.264 encoder for codec {video_codec or 'unknown'}")
        log.info("Encode video to H.264 because codec is %s: %s", video_codec, video_id)
        args = ["-c:v", _encoder, *_encoder_args, "-c:a", "aac", "-b:a", "160k"]
    if video_id in _abandoned:
        return
    temp = downloads_dir() / f"{video_id}.encode.mp4"
    if temp.exists():
        temp.unlink()
    _run_ffmpeg(source, temp, args)
    temp.replace(final)
    if source.exists() and source.resolve() != final.resolve():
        source.unlink()


async def _run_download(video_id: str, budget: float) -> None:
    """Download one id. A positive budget is the deadline; zero waits it out.

    Zero is an empty buffer. Capping that at zero seconds would stop the queue
    from ever filling. A stuck socket still dies via yt-dlp's socket timeout.
    """
    _abandoned.discard(video_id)
    if budget > 0:
        await asyncio.wait_for(asyncio.to_thread(_download_mp4, video_id), timeout=budget)
        return
    await asyncio.to_thread(_download_mp4, video_id)


def _download_mp4(video_id: str) -> None:
    import yt_dlp

    ensure_video_dirs()
    outtmpl = str(downloads_dir() / "%(id)s.%(ext)s")
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
        "socket_timeout": _SOCKET_TIMEOUT_SEC,
        "windowsfilenames": True,
    }
    cookiefile = yt_cookies.netscape_path()
    if cookiefile is None:
        raise RuntimeError("YouTube cookies are not valid")
    opts["cookiefile"] = str(cookiefile)

    def _stop_when_held(_status: dict) -> None:
        if _download_hold > 0 or video_id in _abandoned:
            raise yt_dlp.utils.DownloadCancelled("fila ainda não randomizada")

    opts["progress_hooks"] = [_stop_when_held]
    url = f"https://www.youtube.com/watch?v={video_id}"
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.download([url])
    if video_id in _abandoned:
        return
    # yt-dlp moves the file to the outtmpl after its own postprocessors.
    # Placing it in queue has to happen after that, or it is moved back.
    _place_in_queue(video_id)


async def discard_all_downloads() -> None:
    """Delete every queued MP4 and forget the stored paths."""
    protect_video_ids(set())
    async with get_db() as db:
        cursor = await db.execute("SELECT video_id, local_path FROM queue")
        rows = [dict(row) for row in await cursor.fetchall()]
        await db.execute("UPDATE queue SET local_path = '', download_error = ''")
        await db.commit()
    doomed: list[Path] = []
    for row in rows:
        doomed.extend(_leftovers(row["video_id"]))
        if row["local_path"]:
            doomed.append(Path(row["local_path"]))
    doomed.extend(files_outside_keep(downloads_dir(), set()))
    doomed.extend(files_outside_keep(queue_dir(), set()))
    doomed.extend(files_outside_keep(settings.VIDEOS_DIR, set()))
    await remove_files(doomed)


async def _clear_finishable_errors(rows: list[dict]) -> None:
    """A raw file left in downloads can still be moved. The old error blocked that."""
    ready = [
        row["video_id"] for row in rows
        if row.get("download_error") and raw_download(row["video_id"])
    ]
    if not ready:
        return
    async with get_db() as db:
        for video_id in ready:
            await db.execute(
                "UPDATE queue SET download_error = '' WHERE video_id = ?",
                (video_id,),
            )
        await db.commit()
    for row in rows:
        if row["video_id"] in ready:
            row["download_error"] = ""
    log.info("Cleared download errors for %d files waiting in downloads", len(ready))


async def _clear_missing_paths(rows: list[dict]) -> None:
    """Drop stored paths whose files are gone so the badge and the window agree."""
    missing = []
    for row in rows:
        path = row.get("local_path") or ""
        if path and not Path(path).is_file():
            missing.append(row["video_id"])
            row["local_path"] = ""
    if not missing:
        return
    async with get_db() as db:
        for video_id in missing:
            await db.execute(
                "UPDATE queue SET local_path = '' WHERE video_id = ?",
                (video_id,),
            )
        await db.commit()
    log.info("Cleared %d queue paths whose files are gone", len(missing))


async def _queue_rows() -> list[dict]:
    async with get_db() as db:
        cursor = await db.execute(
            "SELECT id, video_id, status, published_unix, local_path, play_order, duration, download_error FROM queue"
        )
        return [dict(row) for row in await cursor.fetchall()]


async def _forget_paths_outside(keep: set[str]) -> None:
    """Forget the file path of videos outside the window. Their error stays.

    Clearing download_error here is what parked the cycle: the failed video
    lost its block, became downloadable again, was picked as next in play order,
    and the player waited 5s by 5s for a file that had just failed.
    """
    async with get_db() as db:
        cursor = await db.execute(
            "SELECT video_id, local_path FROM queue WHERE local_path != ''"
        )
        rows = await cursor.fetchall()
        for row in rows:
            if row["video_id"] in keep:
                continue
            path = Path(row["local_path"]) if row["local_path"] else None
            if path is not None:
                await remove_files([path])
            await db.execute(
                "UPDATE queue SET local_path = '' WHERE video_id = ?",
                (row["video_id"],),
            )
        await db.commit()


async def retry_error_videos() -> int:
    """Clear download errors so videos whose turn came round are tried again.

    Called when the queue wraps. A row is retried only when the error is the
    one thing blocking it — a stale or over-long video keeps its block.
    """
    rows = await _queue_rows()
    ready = [
        row["video_id"] for row in rows
        if row.get("download_error")
        and not download_blocked({**row, "download_error": ""})
    ]
    if not ready:
        return 0
    async with get_db() as db:
        for video_id in ready:
            await db.execute(
                "UPDATE queue SET download_error = '' WHERE video_id = ?",
                (video_id,),
            )
        await db.commit()
    log.info("Retrying %d videos whose turn came round again", len(ready))
    schedule_sync()
    return len(ready)


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


async def reset_video_error(queue_id: int) -> bool:
    """Clear download error for a video so sync will try again."""
    async with get_db() as db:
        await db.execute(
            "UPDATE queue SET download_error = '' WHERE id = ?",
            (queue_id,),
        )
        await db.commit()
    schedule_sync()
    return True
