"""H.264 stays a remux. Other codecs still scale when the frame is not the canvas."""

from app.queue.download import needs_video_scale

CANVAS = (1920, 1080)


def test_h264_is_not_scaled_even_when_the_frame_differs():
    assert needs_video_scale("h264", 1280, 720, CANVAS) is False
    assert needs_video_scale("h264", 1920, 1080, CANVAS) is False


def test_other_codecs_scale_only_when_the_frame_differs():
    assert needs_video_scale("vp9", 1280, 720, CANVAS) is True
    assert needs_video_scale("vp9", 1920, 1080, CANVAS) is False
    assert needs_video_scale("", 1280, 720, CANVAS) is True
    assert needs_video_scale("vp9", 0, 0, CANVAS) is False
