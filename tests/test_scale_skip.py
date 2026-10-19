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


def test_1080p_horizontal_video_action_without_and_with_normalization():
    from app.queue.download import placement_action

    # 1080p H.264 sem normalização: deve apenas mover o arquivo (sem reencode)
    action_move = placement_action(
        video_codec="h264",
        audio_codec="aac",
        suffix=".mp4",
        needs_scale=False,
        normalize=False,
        measured=None,
        encoder="libx264",
        encoder_args=("-preset", "veryfast"),
        scale_filter="scale=1920:1080",
        portrait=False,
    )
    assert action_move == "move"

    # 1080p H.264 com normalização: copia vídeo sem reencode (-c:v copy) e processa áudio
    measured = {
        "input_i": "-20.0",
        "input_tp": "-2.0",
        "input_lra": "7.0",
        "input_thresh": "-30.0",
        "target_offset": "6.0",
    }
    action_normalize = placement_action(
        video_codec="h264",
        audio_codec="aac",
        suffix=".mp4",
        needs_scale=False,
        normalize=True,
        measured=measured,
        encoder="libx264",
        encoder_args=("-preset", "veryfast"),
        scale_filter="scale=1920:1080",
        portrait=False,
    )
    assert "-c:v" in action_normalize
    assert action_normalize[action_normalize.index("-c:v") + 1] == "copy"
    assert "-af" in action_normalize
    assert "-c:a" in action_normalize and action_normalize[action_normalize.index("-c:a") + 1] == "aac"

