"""Cut SponsorBlock sponsor skips out of a downloaded file before it is queued.

A miss, a weak mark, or a failed encode keeps the original file. Playback never seeks.
"""
from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

_API = "https://sponsor.ajay.app/api/skipSegments"
_TIMEOUT_SEC = 5.0
# News open and close stay in the file even when marked as sponsor.
_HEAD_SEC = 45.0
_TAIL_SEC = 20.0
_MAX_SEGMENT_SEC = 60.0
_MAX_TOTAL_SEC = 90.0
_MAX_FRACTION = 0.10
_MIN_REMAIN_SEC = 30.0
_MIN_REMAIN_FRACTION = 0.60
_DURATION_TOLERANCE_SEC = 2.0


@dataclass(frozen=True)
class Decision:
    uuid: str
    start: float
    end: float
    accepted: bool
    reason: str


def flatten_segments(payload: object) -> list[dict] | None:
    """Accept the flat skipSegments list or the hashed grouped shape."""
    if not isinstance(payload, list):
        return None
    flat: list[dict] = []
    for item in payload:
        if not isinstance(item, dict):
            return None
        if "segment" in item:
            flat.append(item)
            continue
        nested = item.get("segments")
        if not isinstance(nested, list):
            return None
        for segment in nested:
            if not isinstance(segment, dict):
                return None
            flat.append(segment)
    return flat


def _interval(item: dict) -> tuple[float, float] | None:
    raw = item.get("segment")
    if not isinstance(raw, (list, tuple)) or len(raw) != 2:
        return None
    try:
        start = float(raw[0])
        end = float(raw[1])
    except (TypeError, ValueError):
        return None
    if start < 0 or start >= end:
        return None
    return start, end


def _agreed(item: dict) -> bool:
    if item.get("locked") in (1, True):
        return True
    try:
        return int(item.get("votes") or 0) >= 2
    except (TypeError, ValueError):
        return False


def choose_cuts(segments: list[dict], duration: float) -> tuple[list[tuple[float, float]], list[Decision]]:
    """Ranges to remove, plus one decision per submitted segment.

    A videoDuration that does not match the file rejects every cut.
    """
    if duration <= 0:
        return [], []
    for item in segments:
        reported = item.get("videoDuration")
        if reported is None:
            continue
        try:
            reported_s = float(reported)
        except (TypeError, ValueError):
            continue
        if abs(reported_s - duration) > _DURATION_TOLERANCE_SEC:
            decisions = []
            for one in segments:
                interval = _interval(one) or (0.0, 0.0)
                decisions.append(Decision(
                    str(one.get("UUID") or ""), interval[0], interval[1], False, "duration",
                ))
            return [], decisions

    candidates: list[tuple[float, float, str]] = []
    pending: list[tuple[str, str, float, float, str]] = []
    for index, item in enumerate(segments):
        uuid = str(item.get("UUID") or "")
        key = str(index)
        interval = _interval(item)
        start, end = interval if interval else (0.0, 0.0)
        reason = _reject_reason(item, interval, duration)
        if reason:
            pending.append((uuid, reason, start, end, key))
            continue
        assert interval is not None
        candidates.append((interval[0], interval[1], key))
        pending.append((uuid, "", start, end, key))

    candidates.sort(key=lambda row: (row[0], row[1]))
    merged = _merge(candidates)
    kept: list[tuple[float, float]] = []
    removed = 0.0
    floor = max(_MIN_REMAIN_SEC, duration * _MIN_REMAIN_FRACTION)
    accepted_ids: set[str] = set()
    refused_ids: set[str] = set()
    for start, end, keys in merged:
        length = end - start
        next_removed = removed + length
        if next_removed > _MAX_TOTAL_SEC or next_removed > duration * _MAX_FRACTION:
            refused_ids.update(keys)
            continue
        if duration - next_removed < floor:
            refused_ids.update(keys)
            continue
        kept.append((start, end))
        removed = next_removed
        accepted_ids.update(keys)

    decisions: list[Decision] = []
    for uuid, reason, start, end, key in pending:
        if reason:
            decisions.append(Decision(uuid, start, end, False, reason))
        elif key in accepted_ids:
            decisions.append(Decision(uuid, start, end, True, "cut"))
        else:
            decisions.append(Decision(uuid, start, end, False, "cap"))
    return kept, decisions


def _reject_reason(item: dict, interval: tuple[float, float] | None, duration: float) -> str:
    if item.get("category") != "sponsor":
        return "category"
    if item.get("actionType") != "skip":
        return "action"
    if interval is None:
        return "range"
    start, end = interval
    if not _agreed(item):
        return "votes"
    if end - start > _MAX_SEGMENT_SEC:
        return "length"
    if start < _HEAD_SEC or end > duration - _TAIL_SEC:
        return "edge"
    if end >= duration:
        return "range"
    return ""


def _merge(rows: list[tuple[float, float, str]]) -> list[tuple[float, float, list[str]]]:
    merged: list[tuple[float, float, list[str]]] = []
    for start, end, uuid in rows:
        if merged and start <= merged[-1][1] + 0.05:
            prev_start, prev_end, uuids = merged[-1]
            uuids.append(uuid)
            merged[-1] = (prev_start, max(prev_end, end), uuids)
            continue
        merged.append((start, end, [uuid]))
    return merged


def fetch_segments(video_id: str) -> list[dict] | None:
    """None when the lookup failed. An empty list means there is nothing to cut."""
    import httpx

    try:
        response = httpx.get(
            _API,
            params={"videoID": video_id, "categories": '["sponsor"]'},
            timeout=_TIMEOUT_SEC,
        )
    except httpx.HTTPError as exc:
        log.warning("SponsorBlock lookup failed for %s: %s", video_id, exc)
        return None
    if response.status_code == 404:
        return []
    if response.status_code != 200:
        log.warning("SponsorBlock returned HTTP %s for %s", response.status_code, video_id)
        return None
    try:
        payload = response.json()
    except ValueError:
        log.warning("SponsorBlock returned broken JSON for %s", video_id)
        return None
    flat = flatten_segments(payload)
    if flat is None:
        log.warning("SponsorBlock returned an unexpected body for %s", video_id)
        return None
    return flat


def media_duration(path: Path) -> float | None:
    cmd = [
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(path),
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.warning("ffprobe duration failed for %s: %s", path.name, exc)
        return None
    if result.returncode != 0:
        return None
    try:
        duration = float((result.stdout or b"").decode().strip())
    except ValueError:
        return None
    if duration <= 0:
        return None
    return duration


def _keep_ranges(duration: float, cuts: list[tuple[float, float]]) -> list[tuple[float, float]]:
    ranges: list[tuple[float, float]] = []
    cursor = 0.0
    for start, end in cuts:
        if start > cursor:
            ranges.append((cursor, start))
        cursor = max(cursor, end)
    if cursor < duration:
        ranges.append((cursor, duration))
    return [(start, end) for start, end in ranges if end - start > 0.05]


def cut_sponsors(source: Path, dest: Path, cuts: list[tuple[float, float]], duration: float) -> None:
    """Write dest with the cut ranges removed. Raises RuntimeError on failure."""
    ranges = _keep_ranges(duration, cuts)
    if len(ranges) < 2 and cuts:
        # A cut that leaves a single piece still needs one trim so the tail is dropped.
        pass
    if not ranges:
        raise RuntimeError("sponsor cut left no media")
    parts: list[str] = []
    labels: list[str] = []
    for index, (start, end) in enumerate(ranges):
        parts.append(f"[0:v]trim=start={start}:end={end},setpts=PTS-STARTPTS[v{index}]")
        parts.append(f"[0:a]atrim=start={start}:end={end},asetpts=PTS-STARTPTS[a{index}]")
        labels.append(f"[v{index}][a{index}]")
    graph = ";".join(parts) + ";" + "".join(labels) + f"concat=n={len(ranges)}:v=1:a=1[v][a]"
    dest.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-i", str(source),
        "-filter_complex", graph,
        "-map", "[v]", "-map", "[a]",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
        "-c:a", "aac", "-b:a", "160k",
        str(dest),
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, timeout=3600)
    except (OSError, subprocess.TimeoutExpired) as exc:
        dest.unlink(missing_ok=True)
        raise RuntimeError(f"ffmpeg sponsor cut failed: {exc}") from exc
    if result.returncode != 0 or not dest.exists() or dest.stat().st_size <= 0:
        err = (result.stderr or b"").decode("utf-8", "replace").strip()
        dest.unlink(missing_ok=True)
        raise RuntimeError(err[-400:] or "ffmpeg sponsor cut produced no MP4")


_REASON = {
    "votes": "votos",
    "length": "longo",
    "edge": "início/fim",
    "cap": "teto",
    "duration": "duração",
    "category": "categoria",
    "action": "ação",
    "range": "intervalo",
}


def sponsor_status(decisions: list[Decision], *, applied: bool, failed: str = "") -> str:
    """One operator line for the lookup outcome."""
    if failed:
        return f"SponsorBlock: corte falhou, arquivo inteiro ({failed})"
    if any(item.reason == "duration" for item in decisions):
        return "SponsorBlock: duração da API não confere, arquivo inteiro"
    accepted = [item for item in decisions if item.accepted and applied]
    refused = [item for item in decisions if not item.accepted and item.reason != "duration"]
    parts: list[str] = []
    if accepted:
        spans = ", ".join(f"{int(item.start)}–{int(item.end)}s" for item in accepted)
        parts.append(f"cortes {spans}")
    if refused:
        names = list(dict.fromkeys(_REASON.get(item.reason, item.reason) for item in refused))
        parts.append("ignorados " + ", ".join(names))
    if not parts:
        return "SponsorBlock: nenhum corte"
    return "SponsorBlock: " + "; ".join(parts)


def prepare_source(video_id: str, source: Path, dest: Path) -> tuple[Path | None, str]:
    """Return dest when a safe cut was written. None keeps source.

    The string is the single SponsorBlock line for the operator.
    """
    segments = fetch_segments(video_id)
    if segments is None:
        return None, "SponsorBlock: erro na consulta, arquivo inteiro"
    if not segments:
        return None, "SponsorBlock: nenhum trecho de patrocínio"
    duration = media_duration(source)
    if duration is None:
        log.warning("SponsorBlock skipped %s: duration unknown", video_id)
        return None, "SponsorBlock: duração desconhecida, arquivo inteiro"
    cuts, decisions = choose_cuts(segments, duration)
    if not cuts:
        return None, sponsor_status(decisions, applied=False)
    try:
        cut_sponsors(source, dest, cuts, duration)
    except RuntimeError as exc:
        log.warning("SponsorBlock kept the full file for %s: %s", video_id, exc)
        return None, sponsor_status(decisions, applied=False, failed=str(exc)[:120])
    return dest, sponsor_status(decisions, applied=True)
