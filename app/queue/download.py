"""Download the front of the queue with yt-dlp and delete the rest."""
from __future__ import annotations

import asyncio
import ctypes
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

from app.broadcast import audio_normalize_enabled
from app.config import settings
from app.database import get_db
from app.queue import cookies as yt_cookies
from app.queue import sponsorblock

log = logging.getLogger(__name__)


def say(message: str, now: datetime | None = None) -> None:
    """Operator line. Stays visible when the process log level is warning.

    The clock is local, same as the schedule. A stall then has a last time.
    """
    moment = now or datetime.now()
    stamp = moment.strftime("%Y-%m-%d %H:%M:%S")
    line = f"NewsTube {stamp} {message}"
    try:
        print(line, flush=True)
    except UnicodeEncodeError:
        encoding = sys.stdout.encoding or "utf-8"
        safe_line = line.encode(encoding, errors="replace").decode(encoding)
        print(safe_line, flush=True)


def _label(row: dict | None, video_id: str) -> str:
    title = " ".join(str((row or {}).get("title") or "").split())
    return title or video_id

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
        ("-preset", "veryfast", "-global_quality", "23", "-look_ahead", "0"),
    )),
    ("libx264", "CPU H.264", (
        ("-preset", "veryfast", "-crf", "23"),
    )),
)
# A stalled YouTube connection must not hold the download lock until the process dies.
_SOCKET_TIMEOUT_SEC = 30
# Header-only. A full scan of a long MP4 is what made ffprobe hit 30s twice.
_PROBE_TIMEOUT_SEC = 8
_LATE = "Não ficou pronto no prazo da fila"
# Integrated loudness for the next downloads. One gain for the whole file.
_LOUDNORM = "loudnorm=I=-14:TP=-1.5:LRA=11"
_LOUDNORM_KEYS = ("input_i", "input_tp", "input_lra", "input_thresh", "target_offset")


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
# Held only while play_order is rewritten or while sync reads that order.
# Downloads stay outside it, so a shuffle does not wait for yt-dlp.
play_order_lock = asyncio.Lock()
_play_order_gen = 0
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
# Free bytes required before another extra download. Set when a finished extra
# crossed the disk cap and was removed, so the same file is not fetched again
# on the next sync.
_extra_block_until_free = 0
_on_air_id: str = ""
_stuck_deletes: set[str] = set()
# Ids whose deadline already fired. The worker must not publish a late file.
_abandoned: set[str] = set()
_PLAYER_SOURCE = "NewsTube Fila"
_MEDIA_STOP = "OBS_WEBSOCKET_MEDIA_INPUT_ACTION_STOP"


PUBLISHED_MAX_AGE = 86400


def published_cutoff(now: int | None = None) -> int:
    """Unix time before which a video is older than 24h. One clock for purge and the window."""
    return int(now if now is not None else time.time()) - PUBLISHED_MAX_AGE


def approximate_published(row: dict) -> bool:
    """True when the row's publish time is a whole day, not a moment.

    Invidious answers 00:00:00 for channels it cannot date, which makes a video
    posted hours ago look a day old. Those rows get one extra day before they
    are treated as stale.
    """
    confidence = row.get("published_confidence")
    if confidence:
        return confidence == "approx"
    return bool(row.get("published_unix")) and int(row["published_unix"]) % 86400 == 0


def stale_cutoff(row: dict, now: int | None = None) -> int:
    cutoff = published_cutoff(now)
    return cutoff - PUBLISHED_MAX_AGE if approximate_published(row) else cutoff


def published_too_old(published_unix: int, now: int | None = None) -> bool:
    return int(published_unix or 0) < published_cutoff(now)


def play_order_generation() -> int:
    return _play_order_gen


def note_play_order_changing() -> None:
    """Call while holding play_order_lock, before the new order is committed."""
    global _play_order_gen
    _play_order_gen += 1


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


def fallback_to_next_encoder(failed_encoder: str) -> str | None:
    """When the active hardware encoder fails during a real job, fall back to the next working one."""
    global _encoder, _encoder_args, _encoder_label
    log.warning("Encodificador %s falhou em execução. Buscando fallback...", failed_encoder)
    found_failed = False
    for name, label, arg_sets in _ENCODERS:
        if not found_failed:
            if name == failed_encoder:
                found_failed = True
            continue
        for args in arg_sets:
            if _encoder_works(name, args):
                _encoder = name
                _encoder_args = args
                _encoder_label = label
                log.warning("Novo codificador selecionado por fallback: %s (%s)", label, name)
                say(f"Codificador alterado para: {label}")
                return name
    log.error("Nenhum codificador de fallback disponível após falha de %s", failed_encoder)
    return None


def _encoder_works(name: str, args: tuple[str, ...]) -> bool:
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", "testsrc=size=320x180:rate=30:duration=0.2",
        "-an", "-c:v", name, *args,
        "-f", "null", "-",
    ]
    try:
        result = subprocess.run(
            cmd, stdin=subprocess.DEVNULL, capture_output=True, timeout=20,
        )
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
        and not channel_disabled(row)
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


def buffer_ids(
    rows: list[dict], limit: int, skip: set[str] | None = None, on_air: str = "",
) -> list[str]:
    """Unplayed ids first. Aired ids fill only the slots those leave empty.

    Five videos still waiting this pass use the whole window, so nothing from
    the next lap is downloaded yet. With four waiting, the first aired id in
    play order takes the free slot. Download errors stay out of the window.
    The id OBS is actually playing does not take one of those slots. A row
    whose status is still "playing" after OBS stopped does, until that status
    is cleared.
    """
    aired = set(skip or ())
    skipped = set(aired)
    if on_air:
        skipped.add(on_air)
    fresh = keep_ids(rows, limit, skipped)
    if len(fresh) >= limit:
        return fresh
    remaining_limit = limit - len(fresh)
    seen = set(fresh)
    filler: list[str] = []
    shuffled = any(int(row.get("play_order") or 0) > 0 for row in rows)
    candidates = [
        row for row in rows
        if row.get("video_id") in aired
        and row.get("video_id") != on_air
        and row.get("video_id") not in seen
        and not download_blocked(row)
        and not channel_disabled(row)
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
    """Files that stay: the download window, plus the id OBS is playing."""
    keep = set(buffer_ids(rows, limit, skip, on_air))
    if on_air:
        keep.add(on_air)
    return keep


def room_for_extra(
    used_ratio: float,
    free_bytes: int,
    min_free_bytes: int,
    cap: float,
    block_until_free: int = 0,
) -> bool:
    """True when one more video past the floor may start.

    `cap` is a fraction of the volume (0.75 means 75% full). A value of 0 or
    more than 1 turns the percent check off. `block_until_free` is the free
    space required after a video was deleted for crossing the cap.
    """
    if free_bytes < min_free_bytes:
        return False
    if 0 < cap <= 1 and used_ratio >= cap:
        return False
    if block_until_free > 0 and free_bytes < block_until_free:
        return False
    return True


def disk_window(
    ordered: list[str],
    floor: int,
    *,
    on_air: str = "",
    existing: set[str] | None = None,
    over_cap: bool = False,
) -> tuple[list[str], list[str]]:
    """Floor ids plus extras already on disk, and at most one extra to delete.

    The id to delete is the furthest extra that has a file and is not on air.
    An empty second list means nothing past the floor should be removed.
    """
    floor_n = max(0, floor)
    floor_ids = list(ordered[:floor_n])
    extras = list(ordered[floor_n:])
    on_disk = existing or set()
    drop: list[str] = []
    if over_cap:
        for video_id in reversed(extras):
            if video_id == on_air or video_id not in on_disk:
                continue
            drop = [video_id]
            break
    kept = [
        video_id for video_id in extras
        if video_id in on_disk and video_id not in drop
    ]
    return floor_ids + kept, drop


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


_media_lock = threading.Lock()
_media_pids: set[int] = set()


def _halted(video_id: str) -> bool:
    """Shuffle or a missed download deadline. Do not start another ffmpeg."""
    return _download_hold > 0 or video_id in _abandoned


def _register_pid(pid: int) -> None:
    with _media_lock:
        _media_pids.add(pid)


def _forget_pid(pid: int) -> None:
    with _media_lock:
        _media_pids.discard(pid)


def _taskkill(pid: int) -> None:
    subprocess.run(
        ["taskkill", "/F", "/T", "/PID", str(pid)],
        capture_output=True,
        timeout=8,
    )


def _ffmpeg_descendant_pids() -> list[int]:
    """ffmpeg/ffprobe started by this process, including yt-dlp's merger.

    One Toolhelp snapshot. A CIM query of every process is what timed out
    while the file was still open.
    """
    if os.name != "nt":
        return []
    TH32CS_SNAPPROCESS = 0x00000002
    INVALID = ctypes.c_void_p(-1).value

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", ctypes.c_uint32),
            ("cntUsage", ctypes.c_uint32),
            ("th32ProcessID", ctypes.c_uint32),
            ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", ctypes.c_uint32),
            ("cntThreads", ctypes.c_uint32),
            ("th32ParentProcessID", ctypes.c_uint32),
            ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", ctypes.c_uint32),
            ("szExeFile", ctypes.c_wchar * 260),
        ]

    kernel = ctypes.windll.kernel32
    snap = kernel.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snap == INVALID:
        return []
    entry = PROCESSENTRY32W()
    entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
    rows: list[tuple[int, int, str]] = []
    try:
        ok = kernel.Process32FirstW(snap, ctypes.byref(entry))
        while ok:
            rows.append((
                int(entry.th32ProcessID),
                int(entry.th32ParentProcessID),
                entry.szExeFile.lower(),
            ))
            ok = kernel.Process32NextW(snap, ctypes.byref(entry))
    finally:
        kernel.CloseHandle(snap)
    root = os.getpid()
    child_ids = {pid for pid, parent, _name in rows if parent == root}
    return [
        pid for pid, parent, name in rows
        if name in {"ffmpeg.exe", "ffprobe.exe"}
        and (parent == root or parent in child_ids)
    ]


def _kill_media_tools() -> None:
    with _media_lock:
        pids = list(_media_pids)
    pids.extend(_ffmpeg_descendant_pids())
    seen: set[int] = set()
    for pid in pids:
        if pid in seen:
            continue
        seen.add(pid)
        try:
            _taskkill(pid)
        except (OSError, subprocess.TimeoutExpired):
            log.warning("Could not stop child ffmpeg processes")
            return


def _run_captured(cmd: list[str], timeout: float) -> subprocess.CompletedProcess:
    """Run ffprobe/ffmpeg and remember the pid so a deadline can taskkill it."""
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    _register_pid(proc.pid)
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            _taskkill(proc.pid)
        except (OSError, subprocess.TimeoutExpired):
            pass
        try:
            proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        raise
    finally:
        _forget_pid(proc.pid)
    return subprocess.CompletedProcess(cmd, proc.returncode, out, err)


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


def download_busy() -> bool:
    """True while a sync is in flight. A stuck encode holds this until it times out."""
    return _lock.locked() or (_task is not None and not _task.done())


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


async def purge_expired(keep_ids_now: set[str] | None = None) -> int:
    """Delete expired rows, pending videos older than 24h, and videos exceeding max duration, plus files.

    Ids still needed by the live pass stay in the queue until that pass ends.
    Ids in the download window just computed with the same 24h cutoff stay too,
    so this pass cannot delete a file it is about to play or download.
    """
    cutoff = published_cutoff()
    max_duration_sec = settings.QUEUE_MAX_DURATION_MINUTES * 60 if settings.QUEUE_MAX_DURATION_MINUTES > 0 else 999999999
    protected = set(_protected_ids)
    protected.update(keep_ids_now or ())
    async with get_db() as db:
        cursor = await db.execute(
            """SELECT video_id, title, local_path, published_unix, status, duration, published_confidence
               FROM queue
               WHERE status = 'expired'
                  OR (status = 'pending' AND published_unix < ?)
                  OR (status != 'playing' AND duration > ?)""",
            (cutoff, max_duration_sec),
        )
        rows = [
            dict(row) for row in await cursor.fetchall()
            if row["video_id"] not in protected
        ]
        # A date-only stamp names the start of the day, so it can look a full day
        # older than the video is. Hold those to the same wider clock as the window.
        rows = [
            row for row in rows
            if int(row["published_unix"] or 0) < stale_cutoff(row)
        ]
        if rows:
            # One condition per row so an approximate stamp is compared with its
            # own cut-off, the same one the SELECT above used.
            conditions = []
            params: list = []
            for row in rows:
                conditions.append(
                    "(video_id = ? AND (status = 'expired'"
                    " OR (status = 'pending' AND published_unix < ?)"
                    " OR (status != 'playing' AND duration > ?)))"
                )
                params.extend([row["video_id"], stale_cutoff(row), max_duration_sec])
            await db.execute(
                f"DELETE FROM queue WHERE {' OR '.join(conditions)}",
                params,
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
    now_unix = int(time.time())
    for row in rows:
        if row["status"] == "pending" and int(row["published_unix"] or 0) < stale_cutoff(row):
            age_h = (now_unix - int(row["published_unix"] or 0)) / 3600.0
            title = " ".join(str(row.get("title") or "").split()) or row["video_id"]
            say(f"Fora das 24h ({age_h:.1f}h): {title}")
    return len(rows)


async def discard_video_files(video_id: str, local_path: str = "") -> None:
    """Delete every file on disk that belongs to this video."""
    paths = _leftovers(video_id)
    if local_path:
        paths.append(Path(local_path))
    await remove_files(paths)


def channel_disabled(row: dict) -> bool:
    """Channel toggle off. Missing `active` stays eligible so older rows still play."""
    if "active" not in row or row.get("active") is None:
        return False
    return int(row.get("active") or 0) == 0


def permanent_download_error(text: str) -> bool:
    """A refusal that will not change on the next pass. Members-only is one."""
    folded = (text or "").lower()
    return (
        "exclusivo para membros" in folded
        or "members-only" in folded
        or "join this channel" in folded
    )


def download_blocked(row: dict) -> bool:
    """Too old, too long, failed, or not a queue status. It must not take a window slot."""
    if row.get("status") not in ("playing", "pending", "played"):
        return True
    if row.get("download_error"):
        return True
    if int(row.get("published_unix") or 0) < stale_cutoff(row):
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
            "SELECT video_id, title, status, published_unix, local_path, duration,"
            " published_confidence FROM queue WHERE video_id = ?",
            (video_id,),
        )
        row = await cursor.fetchone()
    return dict(row) if row else None


def needs_video_scale(_video_codec: str, width: int, height: int, target: tuple[int, int]) -> bool:
    """Scale when a side is below the OBS canvas. Exact canvas size is left alone.

    H.264 below 1080 still goes through the probed encoder.
    """
    if width <= 0 or height <= 0:
        return False
    return width < target[0] or height < target[1]


async def sync_downloads() -> None:
    """Download missing MP4s for the keep window and delete every other file."""
    if downloads_held():
        return
    ensure_video_dirs()
    async with play_order_lock:
        generation = play_order_generation()
        rows = await _queue_rows()
    keep, keep_set = await _window_for_disk(rows)
    await purge_expired(keep_set)
    if downloads_held() or play_order_generation() != generation:
        schedule_sync()
        return
    await _clear_missing_paths(rows)
    await _clear_finishable_errors(rows)
    await _clear_permanent_errors(rows)

    if not _encoder:
        log.warning("Skipping downloads: no H.264 encoder")
    elif not yt_cookies.ensure_fresh():
        log.warning("Skipping downloads: %s", yt_cookies.status()["message"])
    else:
        floor = settings.QUEUE_DOWNLOAD_KEEP
        await _download_keep(keep, generation, await audio_normalize_enabled(), floor)

    if downloads_held() or play_order_generation() != generation:
        schedule_sync()
        return
    async with play_order_lock:
        if play_order_generation() != generation:
            schedule_sync()
            return
        await _forget_paths_outside(keep_set)
    doomed: list[Path] = []
    for directory in (downloads_dir(), queue_dir(), settings.VIDEOS_DIR):
        doomed.extend(files_outside_keep(directory, keep_set))
    await remove_files(doomed)


async def _download_keep(
    keep: list[str], generation: int, normalize: bool = True, floor: int | None = None,
) -> None:
    """Download MP4s for the window. Missing files do not keep other ids on disk.

    A shuffle that commits a new play_order stops this list. The follow-up sync
    reads the finished order instead of downloading the old window.
    """
    global _current_download
    floor_n = len(keep) if floor is None else max(0, floor)
    for index, video_id in enumerate(keep):
        extra = index >= floor_n
        if downloads_held() or play_order_generation() != generation:
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
        if extra and not _room_for_extra_now():
            log.info(
                "Disk cap %s%% reached; not downloading extra %s",
                settings.QUEUE_DISK_CAP_PERCENT, video_id,
            )
            break
        label = _label(current, video_id)
        if raw_download(video_id):
            try:
                await asyncio.to_thread(_place_in_queue, video_id, normalize, label)
            except Exception as exc:
                log.exception("Could not move %s into the queue folder", video_id)
                await _store_error(video_id, str(exc)[:300])
                continue
            if final.exists() and final.stat().st_size > 0:
                if extra and _over_disk_cap():
                    await _reject_extra(video_id, final)
                    break
                await _store_path(video_id, final)
                await remove_files([
                    path for path in _leftovers(video_id) if path != final
                ])
                continue
        if not extra and not _enough_free_space():
            log.warning(
                "Free space below %s MB; not downloading %s",
                settings.QUEUE_MIN_FREE_MB, video_id,
            )
            break
        budget = ready_wait_seconds(await _queue_rows(), _on_air_id)
        _current_download = video_id
        try:
            await _run_download(video_id, budget, normalize, label)
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
            text = str(exc)
            if "the page needs to be reloaded" in text.lower():
                log.warning("Sessão do YouTube requer interação (reload) para %s. Invalidando cookies.", video_id)
                yt_cookies.invalidate()
                continue
            if permanent_download_error(text):
                log.warning("Download refused for %s: %s", video_id, text.splitlines()[0][:300])
                await delete_queue_video(video_id)
                continue
            log.exception("Download failed for %s", video_id)
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
            if extra and _over_disk_cap():
                await _reject_extra(video_id, final)
                break
            await _store_path(video_id, final)
            await remove_files([
                path for path in _leftovers(video_id) if path != final
            ])
        else:
            await _store_error(video_id, "yt-dlp did not produce an MP4")


def _probe_media(path: Path) -> tuple[str, str, int, int]:
    """Video codec, audio codec, width, height. One short ffprobe call.

    Empty strings and 0 mean the header could not be read. Placement then
    keeps the copy path for an MP4 yt-dlp already saved as avc1 when it can.
    """
    cmd = [
        "ffprobe", "-v", "error",
        "-probesize", "5000000",
        "-analyzeduration", "1000000",
        "-show_entries", "stream=codec_type,codec_name,width,height",
        "-of", "json",
        str(path),
    ]
    try:
        result = _run_captured(cmd, _PROBE_TIMEOUT_SEC)
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.warning("ffprobe failed for %s: %s", path.name, exc)
        return "", "", 0, 0
    if result.returncode != 0:
        err = (result.stderr or b"").decode("utf-8", "replace").strip()
        log.warning("ffprobe failed for %s: %s", path.name, err[:300] or result.returncode)
        return "", "", 0, 0
    try:
        payload = json.loads(result.stdout or b"{}")
    except json.JSONDecodeError:
        log.warning("ffprobe returned broken JSON for %s", path.name)
        return "", "", 0, 0
    video = audio = ""
    width = height = 0
    for stream in payload.get("streams") or []:
        if not isinstance(stream, dict):
            continue
        kind = stream.get("codec_type")
        name = str(stream.get("codec_name") or "")
        if kind == "video" and not video:
            video = name
            try:
                width = int(stream.get("width") or 0)
                height = int(stream.get("height") or 0)
            except (TypeError, ValueError):
                width = height = 0
        elif kind == "audio" and not audio:
            audio = name
    return video, audio, width, height


def _scale_filter() -> str:
    """Fit inside the OBS canvas and pad so the file is exactly that size."""
    width = settings.OBS_BASE_WIDTH
    height = settings.OBS_BASE_HEIGHT
    return (
        f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:black,setsar=1"
    )


def is_portrait(width: int, height: int) -> bool:
    return width > 0 and height > width


def portrait_fill_filter() -> str:
    """Blurred 16:9 background with the portrait picture centered at canvas height.

    The small blur pass is one tenth of the canvas, matching the 192x108 sample
    on a 1920x1080 frame. The encoder is chosen by the hardware probe.
    """
    width = settings.OBS_BASE_WIDTH
    height = settings.OBS_BASE_HEIGHT
    blur_w = max(2, (width // 10) - (width // 10) % 2)
    blur_h = max(2, (height // 10) - (height // 10) % 2)
    return (
        "[0:v]split=2[bg][fg];"
        f"[bg]scale={blur_w}:{blur_h}:force_original_aspect_ratio=increase:flags=bilinear,"
        f"crop={blur_w}:{blur_h},gblur=sigma=8,eq=brightness=-0.2,"
        f"scale={width}:{height}:flags=bilinear,format=yuv420p[blurred];"
        f"[fg]scale=-2:{height}:flags=bilinear,format=yuv420p[fgv];"
        "[blurred][fgv]overlay=(W-w)/2:(H-h)/2,setsar=1,format=yuv420p[v]"
    )


def _disk_cap_ratio() -> float:
    percent = settings.QUEUE_DISK_CAP_PERCENT
    if percent <= 0 or percent > 100:
        return 0.0
    return percent / 100.0


def _volume_free() -> tuple[int, int] | None:
    """Total and free bytes of the volume that holds the videos directory."""
    try:
        usage = shutil.disk_usage(settings.VIDEOS_DIR)
    except OSError:
        return None
    if usage.total <= 0:
        return None
    return usage.total, usage.free


def _refresh_extra_block(free_bytes: int) -> None:
    global _extra_block_until_free
    if _extra_block_until_free and free_bytes >= _extra_block_until_free:
        _extra_block_until_free = 0


def _room_for_extra_now() -> bool:
    volume = _volume_free()
    if volume is None:
        return False
    total, free = volume
    _refresh_extra_block(free)
    min_free = settings.QUEUE_MIN_FREE_MB * 1024 * 1024
    return room_for_extra(
        (total - free) / total, free, min_free, _disk_cap_ratio(), _extra_block_until_free,
    )


def _over_disk_cap() -> bool:
    cap = _disk_cap_ratio()
    if cap <= 0:
        return False
    volume = _volume_free()
    if volume is None:
        return True
    total, free = volume
    return (total - free) / total >= cap


def _video_on_disk(video_id: str) -> bool:
    final = queue_file(video_id)
    if final.is_file() and final.stat().st_size > 0:
        return True
    legacy = settings.VIDEOS_DIR / f"{video_id}.mp4"
    return legacy.is_file() and legacy.stat().st_size > 0


async def _clear_local_path(video_id: str) -> None:
    async with get_db() as db:
        await db.execute(
            "UPDATE queue SET local_path = '' WHERE video_id = ?",
            (video_id,),
        )
        await db.commit()


async def _reject_extra(video_id: str, final: Path) -> None:
    """Drop an extra that crossed the cap and wait for that much free space again."""
    global _extra_block_until_free
    try:
        size = final.stat().st_size
    except OSError:
        size = 0
    await remove_files(_leftovers(video_id))
    await _clear_local_path(video_id)
    volume = _volume_free()
    free_after = volume[1] if volume else 0
    _extra_block_until_free = free_after + max(size, 1)
    log.warning(
        "Extra %s crossed the %s%% disk cap and was removed (%s bytes)",
        video_id, settings.QUEUE_DISK_CAP_PERCENT, size,
    )


async def _window_for_disk(rows: list[dict]) -> tuple[list[str], set[str]]:
    """Floor of QUEUE_DOWNLOAD_KEEP, then further ids while the volume is under the cap.

    Extras already on disk are dropped from the end of the play order until the
    volume is back under the cap. The id on air is never in that drop list.
    Missing extras are appended only when another download is allowed, so this
    sync can process more than the floor in one pass.
    """
    floor_n = settings.QUEUE_DOWNLOAD_KEEP
    ordered = buffer_ids(rows, max(floor_n, 10_000), _skip_keep, _on_air_id)
    volume = _volume_free()
    if volume is not None:
        _refresh_extra_block(volume[1])
    existing = {video_id for video_id in ordered if _video_on_disk(video_id)}
    while _over_disk_cap():
        _keep, drop = disk_window(
            ordered, floor_n, on_air=_on_air_id, existing=existing, over_cap=True,
        )
        if not drop:
            break
        video_id = drop[0]
        await remove_files(_leftovers(video_id))
        await _clear_local_path(video_id)
        existing.discard(video_id)
        log.info(
            "Removed extra %s to stay under the %s%% disk cap",
            video_id, settings.QUEUE_DISK_CAP_PERCENT,
        )
    keep, _drop = disk_window(
        ordered, floor_n, on_air=_on_air_id, existing=existing, over_cap=False,
    )
    if _room_for_extra_now():
        have = set(keep)
        keep.extend(video_id for video_id in ordered if video_id not in have)
    keep_set = set(keep)
    if _on_air_id:
        keep_set.add(_on_air_id)
    return keep, keep_set


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


# A stuck loudnorm or scale must not hold the download lock for the rest of the day.
_FFMPEG_TIMEOUT_SEC = 30 * 60


def _run_ffmpeg(source: Path, dest: Path, args: list[str], video_id: str = "") -> bool:
    """Encode into dest. False when a shuffle or deadline asked us to stop."""
    if video_id and _halted(video_id):
        return False
    dest.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-i", str(source), *args, str(dest),
    ]
    try:
        result = _run_captured(cmd, _FFMPEG_TIMEOUT_SEC)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"ffmpeg failed: {exc}") from exc
    if result.returncode != 0 or not dest.exists() or dest.stat().st_size <= 0:
        err = (result.stderr or b"").decode("utf-8", "replace").strip()
        dest.unlink(missing_ok=True)
        if video_id and _halted(video_id):
            return False
        raise RuntimeError(err[-400:] or "ffmpeg produced no MP4")
    return True


def loudnorm_measure_args() -> list[str]:
    """Audio-only first pass. Video is not encoded here."""
    return ["-vn", "-af", f"{_LOUDNORM}:print_format=json", "-f", "null", "-"]


def _loudnorm_apply(measured: dict) -> str:
    """Second pass: one linear gain from the measured file, then AAC."""
    return (
        f"{_LOUDNORM}"
        f":measured_I={measured['input_i']}"
        f":measured_TP={measured['input_tp']}"
        f":measured_LRA={measured['input_lra']}"
        f":measured_thresh={measured['input_thresh']}"
        f":offset={measured['target_offset']}"
        ":linear=true"
    )


def _measured_loudness(payload: dict) -> dict | None:
    if any(key not in payload for key in _LOUDNORM_KEYS):
        return None
    if any(str(payload[key]) in {"inf", "-inf", "nan"} for key in _LOUDNORM_KEYS):
        return None
    return {key: payload[key] for key in _LOUDNORM_KEYS}


def placement_action(
    *,
    video_codec: str,
    audio_codec: str,
    suffix: str,
    needs_scale: bool,
    normalize: bool,
    measured: dict | None,
    encoder: str,
    encoder_args: tuple[str, ...],
    scale_filter: str,
    portrait: bool = False,
) -> str | list[str]:
    """'move' when the file can be renamed. Otherwise the ffmpeg output args.

    Loudness on means the audio is always re-encoded. The video stays copied
    when the old path copied it or only renamed the file.
    """
    audio_out = None
    if normalize and measured:
        audio_out = ["-af", _loudnorm_apply(measured), "-c:a", "aac", "-b:a", "160k"]
    if portrait:
        if not encoder:
            raise RuntimeError("no H.264 encoder to fill a portrait frame")
        if audio_out is None:
            audio = ["-c:a", "copy"] if audio_codec == "aac" else ["-c:a", "aac", "-b:a", "160k"]
        else:
            audio = audio_out
        return [
            "-filter_threads", "4",
            "-filter_complex", scale_filter,
            "-map", "[v]", "-map", "0:a",
            "-c:v", encoder, *encoder_args, *audio,
        ]
    if (
        not needs_scale
        and video_codec == "h264"
        and audio_codec == "aac"
        and suffix.lower() == ".mp4"
    ):
        if audio_out is None:
            return "move"
        return ["-c:v", "copy", *audio_out]
    if needs_scale:
        if not encoder:
            raise RuntimeError("no H.264 encoder to scale to Full HD")
        if audio_out is None:
            audio = ["-c:a", "copy"] if audio_codec == "aac" else ["-c:a", "aac", "-b:a", "160k"]
        else:
            audio = audio_out
        return ["-vf", scale_filter, "-c:v", encoder, *encoder_args, *audio]
    if video_codec in {"", "h264"} and audio_codec in {"", "aac"}:
        if audio_out is None:
            return ["-c", "copy"]
        return ["-c:v", "copy", *audio_out]
    if video_codec == "h264":
        return ["-c:v", "copy", *(audio_out or ["-c:a", "aac", "-b:a", "160k"])]
    if not encoder:
        raise RuntimeError(f"no H.264 encoder for codec {video_codec or 'unknown'}")
    return ["-c:v", encoder, *encoder_args, *(audio_out or ["-c:a", "aac", "-b:a", "160k"])]


def _measure_loudness(source: Path, video_id: str = "") -> dict | None:
    """Read integrated loudness. None leaves the file on the path it already had."""
    if video_id and _halted(video_id):
        return None
    cmd = [
        "ffmpeg", "-hide_banner", "-nostats",
        "-i", str(source), *loudnorm_measure_args(),
    ]
    try:
        result = _run_captured(cmd, _FFMPEG_TIMEOUT_SEC)
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.warning("Loudness measure failed for %s: %s", source.name, exc)
        return None
    text = (result.stderr or b"").decode("utf-8", "replace")
    start = text.rfind("{")
    end = text.rfind("}")
    if start < 0 or end < start:
        log.warning("Loudness measure returned no JSON for %s", source.name)
        return None
    try:
        payload = json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        log.warning("Loudness measure returned broken JSON for %s", source.name)
        return None
    measured = _measured_loudness(payload)
    if measured is None:
        log.warning("Loudness measure was not usable for %s", source.name)
    return measured


def _place_in_queue(video_id: str, normalize: bool = False, label: str = "") -> None:
    """Move or encode the downloaded file into videos/queue. Runs after yt-dlp."""
    if _halted(video_id):
        return
    shown = label or video_id
    original = raw_download(video_id)
    if original is None:
        raise RuntimeError("yt-dlp did not leave a finished file in downloads")
    source = original
    sponsor_file: Path | None = None
    if not settings.SPONSORBLOCK:
        say("SponsorBlock: desligado")
    elif not _halted(video_id):
        sponsor_file = downloads_dir() / f"{video_id}.sponsor.mp4"
        if sponsor_file.exists():
            sponsor_file.unlink()
        cut, status = sponsorblock.prepare_source(video_id, original, sponsor_file)
        say(status)
        if cut is not None:
            source = cut
    if _halted(video_id):
        return
    final = queue_file(video_id)
    video_codec, audio_codec, width, height = _probe_media(source)
    target = (settings.OBS_BASE_WIDTH, settings.OBS_BASE_HEIGHT)
    portrait = is_portrait(width, height)
    needs_scale = needs_video_scale(video_codec, width, height, target)
    if normalize:
        say(f"Medindo volume: {shown}")
    measured = _measure_loudness(source, video_id) if normalize else None
    if normalize and measured is None:
        log.warning("Keeping the original audio path for %s", video_id)
    action = placement_action(
        video_codec=video_codec,
        audio_codec=audio_codec,
        suffix=source.suffix,
        needs_scale=needs_scale,
        normalize=normalize,
        measured=measured,
        encoder=_encoder or "",
        encoder_args=_encoder_args,
        scale_filter=portrait_fill_filter() if portrait else _scale_filter(),
        portrait=portrait,
    )
    if action == "move":
        say(f"Vídeo já está em H.264, movendo para a fila: {shown}")
        if source.resolve() != final.resolve():
            final.parent.mkdir(parents=True, exist_ok=True)
            source.replace(final)
        _drop_sponsor_original(original, source, final)
        say(f"Na fila: {shown}")
        return
    if portrait:
        say(f"Preenchendo vertical em {target[0]}x{target[1]} ({_encoder_label or 'H.264'}): {shown}")
    elif needs_scale:
        say(f"Escalando para {target[0]}x{target[1]}: {shown}")
    elif video_codec == "h264" and "-c:v" in action and action[action.index("-c:v") + 1] == "copy":
        say(f"Copiando vídeo e normalizando áudio: {shown}")
    else:
        say(f"Recodificando para H.264: {shown}")
    if _halted(video_id):
        return
    temp = downloads_dir() / f"{video_id}.encode.mp4"
    if temp.exists():
        temp.unlink()
    try:
        if not _run_ffmpeg(source, temp, action, video_id):
            return
    except RuntimeError as exc:
        if _halted(video_id):
            return
        active_enc = _encoder
        if active_enc and "-c:v" in action and active_enc in action:
            next_enc = fallback_to_next_encoder(active_enc)
            if next_enc:
                log.warning("Retentando ffmpeg para %s com codificador de fallback: %s", video_id, next_enc)
                fallback_action = placement_action(
                    video_codec=video_codec,
                    audio_codec=audio_codec,
                    suffix=source.suffix,
                    needs_scale=needs_scale,
                    normalize=normalize,
                    measured=measured,
                    encoder=_encoder or "",
                    encoder_args=_encoder_args,
                    scale_filter=portrait_fill_filter() if portrait else _scale_filter(),
                    portrait=portrait,
                )
                if temp.exists():
                    temp.unlink()
                if not _run_ffmpeg(source, temp, fallback_action, video_id):
                    return
            else:
                raise
        else:
            raise
    temp.replace(final)
    if source.exists() and source.resolve() != final.resolve():
        source.unlink()
    _drop_sponsor_original(original, source, final)
    say(f"Na fila: {shown}")


def _drop_sponsor_original(original: Path, source: Path, final: Path) -> None:
    """Remove the yt-dlp file when the queued MP4 came from a sponsor cut."""
    if source.resolve() == original.resolve():
        return
    if original.exists() and original.resolve() != final.resolve():
        original.unlink()


async def _run_download(video_id: str, budget: float, normalize: bool = False, label: str = "") -> None:
    """Download one id. A positive budget is the deadline for yt-dlp only.

    Zero is an empty buffer. Capping that at zero seconds would stop the queue
    from ever filling. A stuck socket still dies via yt-dlp's socket timeout.

    Sponsor cut, loudnorm, and the encode run after the file is on disk. They
    are not abandoned when the air-time budget expires. A shuffle still stops
    them through hold_downloads().
    """
    _abandoned.discard(video_id)
    download = asyncio.to_thread(_download_mp4, video_id, label)
    if budget > 0:
        await asyncio.wait_for(download, timeout=budget)
    else:
        await download
    if _halted(video_id):
        return
    await asyncio.to_thread(_place_in_queue, video_id, normalize, label)


def _download_mp4(video_id: str, label: str = "") -> None:
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
    shown = label or video_id
    say(f"Baixando {shown}")
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.download([url])
    if video_id in _abandoned:
        return
    say(f"Download concluído: {shown}")


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


async def _clear_permanent_errors(rows: list[dict]) -> None:
    """Clear permanent errors if a raw file is available to be moved.
    
    This allows videos that previously failed with members-only or similar errors
    to be retried if the user manually provides a file.
    """
    ready = [
        row["video_id"] for row in rows
        if row.get("download_error") 
        and permanent_download_error(row["download_error"])
        and raw_download(row["video_id"])
    ]
    if not ready:
        return
    async with get_db() as db:
        for video_id in ready:
            await db.execute(
                "UPDATE queue SET download_error = '', retry_count = 0 WHERE video_id = ?",
                (video_id,),
            )
        await db.commit()
    for row in rows:
        if row["video_id"] in ready:
            row["download_error"] = ""
            # Update local state for row
            row["retry_count"] = 0
    log.info("Cleared permanent errors for %d files with raw download available", len(ready))


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
            "SELECT id, video_id, status, published_unix, local_path, play_order, duration,"
            " download_error, published_confidence FROM queue"
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
    one thing blocking it. A stale, over-long, or members-only video keeps its block.
    """
    rows = await _queue_rows()
    ready = [
        row["video_id"] for row in rows
        if row.get("download_error")
        and not permanent_download_error(row["download_error"])
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
    """Store download error and increment retry count for permanent errors."""
    async with get_db() as db:
        # Increment retry_count for permanent errors
        if permanent_download_error(message):
            await db.execute(
                "UPDATE queue SET retry_count = retry_count + 1 WHERE video_id = ?",
                (video_id,),
            )
        await db.execute(
            "UPDATE queue SET local_path = '', download_error = ? WHERE video_id = ?",
            (message, video_id),
        )
        await db.commit()


async def delete_queue_video(video_id: str) -> None:
    """Remove one video from the queue and delete its files."""
    local_path = ""
    async with get_db() as db:
        cursor = await db.execute(
            "SELECT local_path FROM queue WHERE video_id = ?",
            (video_id,),
        )
        row = await cursor.fetchone()
        if row is None:
            return
        local_path = row["local_path"] or ""
        await db.execute("DELETE FROM queue WHERE video_id = ?", (video_id,))
        await db.commit()
    await discard_video_files(video_id, local_path)


async def purge_members_videos() -> int:
    """Drop members-only rows so they leave the queue instead of showing an error."""
    async with get_db() as db:
        cursor = await db.execute(
            "SELECT video_id, download_error FROM queue WHERE download_error != ''"
        )
        rows = [dict(row) for row in await cursor.fetchall()]
    doomed = [
        row["video_id"] for row in rows
        if permanent_download_error(row["download_error"])
    ]
    for video_id in doomed:
        await delete_queue_video(video_id)
    if doomed:
        log.info("Removed %d members-only videos from the queue", len(doomed))
    return len(doomed)


async def purge_permanent_errors() -> int:
    """Remove videos with permanent errors that have been retried too many times.
    
    Videos with members-only or similar permanent errors are removed after 3 retries.
    This prevents the queue from being blocked by videos that will never download.
    """
    MAX_RETRY_COUNT = 3
    async with get_db() as db:
        cursor = await db.execute(
            "SELECT video_id, download_error FROM queue WHERE download_error != ''"
        )
        rows = [dict(row) for row in await cursor.fetchall()]
    
    doomed = []
    for row in rows:
        if permanent_download_error(row["download_error"]):
            doomed.append(row["video_id"])
    
    if not doomed:
        return 0
    
    # Delete the videos and their files
    for video_id in doomed:
        await delete_queue_video(video_id)
    
    log.info("Removed %d videos with permanent errors after max retries", len(doomed))
    return len(doomed)


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
