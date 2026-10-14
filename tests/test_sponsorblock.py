"""SponsorBlock cuts stay inside the safe window or the file is left whole."""

import shutil
import subprocess
from pathlib import Path

import pytest

from app.queue.sponsorblock import (
    choose_cuts,
    cut_sponsors,
    flatten_segments,
    media_duration,
    sponsor_status,
)

DURATION = 200.0


def _mark(start, end, **extra):
    item = {
        "category": "sponsor",
        "actionType": "skip",
        "segment": [start, end],
        "UUID": f"{start}-{end}",
        "locked": 0,
        "votes": 3,
        "videoDuration": DURATION,
    }
    item.update(extra)
    return item


def test_no_segments():
    cuts, decisions = choose_cuts([], DURATION)
    assert cuts == []
    assert decisions == []


def test_one_vote_is_not_enough():
    cuts, decisions = choose_cuts([_mark(50, 60, votes=1, locked=0)], DURATION)
    assert cuts == []
    assert decisions[0].reason == "votes"


def test_locked_segment_is_cut():
    cuts, decisions = choose_cuts([_mark(50, 60, votes=0, locked=1)], DURATION)
    assert cuts == [(50.0, 60.0)]
    assert decisions[0].accepted


def test_long_segment_is_refused():
    cuts, decisions = choose_cuts([_mark(50, 120)], DURATION)
    assert cuts == []
    assert decisions[0].reason == "length"


def test_opening_and_closing_stay():
    cuts, decisions = choose_cuts([
        _mark(10, 25, UUID="open"),
        _mark(170, 190, UUID="close"),
    ], DURATION)
    assert cuts == []
    assert [item.reason for item in decisions] == ["edge", "edge"]


def test_duration_mismatch_keeps_the_whole_file():
    cuts, decisions = choose_cuts([_mark(50, 60, videoDuration=DURATION + 5)], DURATION)
    assert cuts == []
    assert decisions[0].reason == "duration"


def test_mute_and_other_categories_are_ignored():
    cuts, decisions = choose_cuts([
        _mark(50, 58, actionType="mute", UUID="mute"),
        _mark(70, 78, category="selfpromo", UUID="promo"),
    ], DURATION)
    assert cuts == []
    assert [item.reason for item in decisions] == ["action", "category"]


def test_two_safe_segments_are_kept_in_order():
    cuts, decisions = choose_cuts([
        _mark(80, 88, UUID="b"),
        _mark(50, 58, UUID="a"),
    ], DURATION)
    assert cuts == [(50.0, 58.0), (80.0, 88.0)]
    assert [item.accepted for item in decisions] == [True, True]


def test_total_cap_drops_the_segment_that_does_not_fit():
    # 10% of 200s is 20s. The third segment would pass 20s.
    cuts, decisions = choose_cuts([
        _mark(50, 60, UUID="a"),
        _mark(70, 80, UUID="b"),
        _mark(90, 100, UUID="c"),
    ], DURATION)
    assert cuts == [(50.0, 60.0), (70.0, 80.0)]
    assert [item.reason for item in decisions] == ["cut", "cut", "cap"]


def test_status_line_names_cuts_and_refusals():
    _, decisions = choose_cuts([
        _mark(50, 60, UUID="a"),
        _mark(10, 20, UUID="open"),
    ], DURATION)
    text = sponsor_status(decisions, applied=True)
    assert text.startswith("SponsorBlock: cortes 50–60s")
    assert "início/fim" in text


def test_grouped_payload_flattens():
    flat = flatten_segments([{"videoID": "abc", "segments": [_mark(50, 60)]}])
    assert flat is not None and flat[0]["segment"] == [50, 60]
    assert flatten_segments({"segment": [1, 2]}) is None


def test_ffmpeg_cut_is_shorter_than_the_source(tmp_path: Path):
    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        pytest.skip("ffmpeg is not installed")
    source = tmp_path / "sponsor-test-src.mp4"
    dest = tmp_path / "sponsor-test-cut.mp4"
    made = subprocess.run([
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", "testsrc=size=320x180:rate=30:duration=70",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=70",
        "-shortest", "-c:v", "libx264", "-preset", "veryfast", "-c:a", "aac",
        str(source),
    ], capture_output=True)
    if made.returncode != 0:
        pytest.skip("could not build a synthetic file")
    try:
        length = media_duration(source)
        assert length is not None and length > 60
        cut_sponsors(source, dest, [(45.0, 50.0)], length)
        cut_length = media_duration(dest)
        assert cut_length is not None
        assert cut_length < length - 3
    finally:
        source.unlink(missing_ok=True)
        dest.unlink(missing_ok=True)
