"""Shorts stay off per channel and portrait frames use the blurred fill."""

import time

from app.config import settings
from app.queue.download import is_portrait, placement_action, portrait_fill_filter
from app.queue.service import pull_shorts_apart, settle_tail_short, split_short_boundary
from app.yt.invidious import is_eligible


def _meta(**extra):
    now = int(time.time())
    meta = {
        "video_id": "abcdefghijk",
        "title": "Um short",
        "duracao": 40,
        "publicacao_unix": now - 7200,
        "tipo": "video",
    }
    meta.update(extra)
    return meta


def test_short_is_rejected_until_the_channel_allows_it():
    meta = _meta()
    now = int(time.time())
    eligible, reason = is_eligible(meta, 0, now_unix=now)
    assert eligible is False
    assert "short" in reason
    eligible, reason = is_eligible(meta, 0, now_unix=now, allow_shorts=True)
    assert eligible is True
    assert reason == "ok"


def test_allowed_short_still_respects_age_and_duration():
    now = int(time.time())
    fresh = _meta(publicacao_unix=now - 60)
    eligible, reason = is_eligible(fresh, 2, now_unix=now, allow_shorts=True)
    assert eligible is False
    assert "Novo demais" in reason
    long = _meta(duracao=50 * 60)
    eligible, reason = is_eligible(long, 0, now_unix=now, allow_shorts=True, max_duration_minutes=30)
    assert eligible is False
    assert "longa" in reason


def test_portrait_fill_matches_the_blur_sample_on_full_hd(monkeypatch):
    monkeypatch.setattr(settings, "OBS_BASE_WIDTH", 1920)
    monkeypatch.setattr(settings, "OBS_BASE_HEIGHT", 1080)
    graph = portrait_fill_filter()
    assert "scale=192:108:force_original_aspect_ratio=increase" in graph
    assert "gblur=sigma=8" in graph
    assert "eq=brightness=-0.2" in graph
    assert "scale=-2:1080:flags=lanczos" in graph
    assert "overlay=(W-w)/2:(H-h)/2" in graph
    assert is_portrait(1080, 1920) is True
    assert is_portrait(1920, 1080) is False


def test_portrait_uses_the_probed_encoder_not_a_fixed_qsv_line():
    graph = "blur-graph"
    args = placement_action(
        video_codec="h264",
        audio_codec="aac",
        suffix=".mp4",
        needs_scale=True,
        normalize=False,
        measured=None,
        encoder="h264_nvenc",
        encoder_args=("-preset", "p4", "-rc", "vbr", "-cq", "23"),
        scale_filter=graph,
        portrait=True,
    )
    assert args[args.index("-filter_complex") + 1] == graph
    assert args[args.index("-c:v") + 1] == "h264_nvenc"
    assert "-preset" in args and "p4" in args
    assert "h264_qsv" not in args
    assert "-c:a" in args and args[args.index("-c:a") + 1] == "copy"


def test_a_normal_video_splits_two_shorts():
    channel = {1: 10, 2: 11, 3: 12}
    assert pull_shorts_apart([1, 2, 3], {1, 2}, channel) == [1, 3, 2]


def test_shorts_stay_together_when_nothing_can_split_them():
    channel = {1: 10, 2: 11}
    assert pull_shorts_apart([1, 2], {1, 2}, channel) == [1, 2]


def test_short_swap_refuses_to_join_the_same_channel():
    channel = {1: 10, 2: 11, 3: 11}
    assert pull_shorts_apart([1, 2, 3], {1, 2}, channel) == [1, 2, 3]


def test_low_group_opens_the_boundary_without_entering_the_tall_group():
    shorts = {1, 2}
    channel = {1: 10, 2: 11, 3: 12}
    high, flat = split_short_boundary([1], [2, 3], shorts, channel)
    assert high == [1]
    assert flat == [3, 2]


def test_tail_short_swaps_with_the_nearest_normal_behind():
    channel = {1: 10, 2: 11, 3: 12}
    assert settle_tail_short([1, 2, 3], {2, 3}, channel) == [2, 1, 3]
