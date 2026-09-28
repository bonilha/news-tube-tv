"""Shuffle keeps 720p and below out of the download window."""

from app.queue.service import shuffle_ids
from app.yt.invidious import stream_max_height


def test_stream_max_height_uses_the_tallest_listed_stream():
    assert stream_max_height({
        "adaptiveFormats": [{"height": 720}, {"height": "1080"}],
        "formatStreams": [{"height": 360}],
    }) == 1080
    assert stream_max_height({}) == 0
    assert stream_max_height({"adaptiveFormats": [{"height": "nope"}]}) == 0


def test_first_window_skips_720_when_taller_videos_fill_it():
    rows = [
        (1, 10, 1080),
        (2, 10, 1080),
        (3, 11, 1080),
        (4, 11, 720),
        (5, 12, 480),
        (6, 12, 1080),
        (7, 13, 0),
        (8, 13, 1080),
    ]
    ordered = shuffle_ids(rows, head=5)
    height = {queue_id: max_height for queue_id, _channel, max_height in rows}
    assert all(not (0 < height[queue_id] <= 720) for queue_id in ordered[:5])
    assert [queue_id for queue_id in ordered if 0 < height[queue_id] <= 720] == ordered[-2:]


def test_720_fills_the_window_only_when_taller_videos_run_out():
    rows = [(1, 1, 1080), (2, 1, 720), (3, 2, 480)]
    ordered = shuffle_ids(rows, head=5)
    assert ordered[0] == 1
    assert set(ordered[1:]) == {2, 3}
