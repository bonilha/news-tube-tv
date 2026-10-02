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


def test_stream_max_height_reads_invidious_size():
    assert stream_max_height({
        "adaptiveFormats": [
            {"type": "audio/mp4", "itag": "140"},
            {"size": "1280x720", "qualityLabel": "720p"},
            {"size": "1920x1080", "qualityLabel": "1080p"},
        ],
    }) == 1080
    assert stream_max_height({
        "formatStreams": [{"size": "202x360", "resolution": "360p"}],
        "adaptiveFormats": [{"size": "1280x720", "qualityLabel": "720p"}],
    }) == 720
    assert stream_max_height({
        "adaptiveFormats": [{"size": "720x1280", "qualityLabel": "720p"}],
    }) == 720


def test_first_window_skips_720_when_taller_videos_fill_it():
    rows = [
        (1, 10, 1080, 0),
        (2, 10, 1080, 0),
        (3, 11, 1080, 0),
        (4, 11, 720, 0),
        (5, 12, 480, 0),
        (6, 12, 1080, 0),
        (7, 13, 0, 0),
        (8, 13, 1080, 0),
    ]
    ordered = shuffle_ids(rows, head=5)
    height = {queue_id: max_height for queue_id, _channel, max_height, _plays in rows}
    assert all(not (0 < height[queue_id] <= 720) for queue_id in ordered[:5])
    assert [queue_id for queue_id in ordered if 0 < height[queue_id] <= 720] == ordered[-2:]


def test_720_fills_the_window_only_when_taller_videos_run_out():
    rows = [(1, 1, 1080, 0), (2, 1, 720, 0), (3, 2, 480, 0)]
    ordered = shuffle_ids(rows, head=5)
    assert ordered[0] == 1
    assert set(ordered[1:]) == {2, 3}


def test_play_count_orders_inside_each_resolution_group():
    rows = [
        (1, 1, 1080, 2),
        (2, 2, 1080, 0),
        (3, 3, 1080, 1),
        (4, 4, 720, 1),
        (5, 5, 480, 0),
    ]
    ordered = shuffle_ids(rows, head=5)
    assert ordered[:3] == [2, 3, 1]
    assert ordered[3:] == [5, 4]
