"""NewsTube Fila keeps the rectangle the editor sent."""

from app.obs.manager import reported_item_size, video_box_transform


def test_video_box_fits_inside_the_page_rectangle():
    transform = video_box_transform(120, 80, 1280, 720, 0)
    assert transform["positionX"] == 120
    assert transform["positionY"] == 80
    assert transform["boundsType"] == "OBS_BOUNDS_SCALE_INNER"
    assert transform["boundsWidth"] == 1280
    assert transform["boundsHeight"] == 720
    assert transform["boundsAlignment"] == 0
    assert transform["alignment"] == 5
    assert transform["scaleX"] == 1
    assert transform["scaleY"] == 1
    assert transform["rotation"] == 0


def test_editor_reads_the_designated_box_not_the_letterboxed_picture():
    width, height = reported_item_size({
        "boundsType": "OBS_BOUNDS_SCALE_INNER",
        "boundsWidth": 1280,
        "boundsHeight": 720,
        "width": 405,
        "height": 720,
        "sourceWidth": 1080,
        "sourceHeight": 1920,
        "scaleX": 1,
        "scaleY": 1,
    })
    assert (width, height) == (1280, 720)


def test_a_source_without_bounds_still_reports_its_scaled_size():
    width, height = reported_item_size({
        "boundsType": "OBS_BOUNDS_NONE",
        "sourceWidth": 1920,
        "sourceHeight": 1080,
        "scaleX": 0.5,
        "scaleY": 0.5,
    })
    assert (width, height) == (960, 540)
