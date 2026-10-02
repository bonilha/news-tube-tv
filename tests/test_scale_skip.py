"""Frames smaller than the OBS canvas are scaled, including H.264."""

from app.queue.download import needs_video_scale

CANVAS = (1920, 1080)


def test_below_canvas_is_scaled_even_for_h264():
    assert needs_video_scale("h264", 1280, 720, CANVAS) is True
    assert needs_video_scale("h264", 720, 1280, CANVAS) is True
    assert needs_video_scale("vp9", 1280, 720, CANVAS) is True
    assert needs_video_scale("", 1280, 720, CANVAS) is True


def test_canvas_size_and_unknown_size_are_not_scaled():
    assert needs_video_scale("h264", 1920, 1080, CANVAS) is False
    assert needs_video_scale("vp9", 1920, 1080, CANVAS) is False
    assert needs_video_scale("vp9", 0, 0, CANVAS) is False
    assert needs_video_scale("h264", 2560, 1440, CANVAS) is False
