"""Download window and which row the cycle picks next."""

import time

from app.cycle import next_in_pass, tail_scan_due
from app.queue.download import buffer_ids, disk_window, retain_ids, room_for_extra


def _row(video_id: str, play_order: int, **extra) -> dict:
    row = {
        "id": play_order,
        "video_id": video_id,
        "status": "pending",
        "published_unix": int(time.time()),
        "play_order": play_order,
        "duration": 60,
        "download_error": "",
    }
    row.update(extra)
    return row


def test_disabled_channel_stays_out_of_the_pass_and_the_window():
    rows = [
        _row("on", 1, active=0),
        _row("off", 2, active=0),
        _row("next", 3, active=1),
    ]
    video, wrapped = next_in_pass(rows, set())
    assert video["video_id"] == "next"
    assert wrapped is False
    assert buffer_ids(rows, 5, set()) == ["next"]
    still, _wrapped = next_in_pass(rows, set(), playing_id="on")
    assert still["video_id"] == "on"
    assert "on" not in buffer_ids(rows, 5, set())
    assert retain_ids(rows, 5, set(), on_air="on") == {"next", "on"}


def test_five_unplayed_do_not_pull_the_next_lap():
    rows = [
        _row("d", 4),
        _row("e", 5),
        _row("f", 6),
        _row("g", 7),
        _row("h", 8),
        _row("a", 9),
        _row("b", 10),
        _row("c", 11),
    ]
    aired = {"a", "b", "c"}
    assert buffer_ids(rows, 5, aired) == ["d", "e", "f", "g", "h"]


def test_three_unplayed_take_the_first_two_of_the_next_lap():
    rows = [
        _row("f", 6),
        _row("g", 7),
        _row("h", 8),
        _row("a", 9),
        _row("b", 10),
        _row("c", 11),
    ]
    aired = {"a", "b", "c"}
    assert buffer_ids(rows, 5, aired) == ["f", "g", "h", "a", "b"]


def test_on_air_does_not_take_a_buffer_slot():
    rows = [_row(video_id, order, status="playing" if video_id == "a" else "pending")
            for order, video_id in enumerate("abcdef", 1)]
    assert buffer_ids(rows, 5, set(), on_air="a") == ["b", "c", "d", "e", "f"]
    assert retain_ids(rows, 5, set(), on_air="a") == {"a", "b", "c", "d", "e", "f"}
    # Status playing without OBS on that file still takes a slot.
    assert buffer_ids(rows, 5, set(), on_air="")[0] == "a"


def test_on_air_stays_when_it_is_outside_the_window():
    rows = [
        _row("b", 2),
        _row("c", 3),
        _row("d", 4),
        _row("e", 5),
        _row("f", 6),
        _row("a", 7),
    ]
    keep = retain_ids(rows, 5, {"a"}, on_air="a")
    assert keep == {"b", "c", "d", "e", "f", "a"}
    assert "only-on-disk" not in keep


def test_sixth_unplayed_is_outside_even_if_its_file_is_still_downloading():
    rows = [_row(video_id, order) for order, video_id in enumerate("abcdef", 1)]
    keep = retain_ids(rows, 5, set())
    assert keep == {"a", "b", "c", "d", "e"}
    assert "f" not in keep


def test_video_older_than_a_day_does_not_take_a_slot_or_block_playback():
    now = int(time.time())
    rows = [
        _row("stale", 1, published_unix=now - 90000),
        _row("a", 2),
        _row("b", 3),
        _row("c", 4),
        _row("d", 5),
        _row("e", 6),
    ]
    assert buffer_ids(rows, 5, set()) == ["a", "b", "c", "d", "e"]
    video, wrapped = next_in_pass(rows, set())
    assert wrapped is False
    assert video["video_id"] == "a"


def test_missing_file_does_not_skip_to_an_aired_download():
    rows = [
        _row("new", 1, local_path=""),
        _row("old", 2, local_path="videos/old.mp4"),
    ]
    video, wrapped = next_in_pass(rows, {"old"})
    assert wrapped is False
    assert video["video_id"] == "new"


def test_empty_unplayed_list_starts_the_same_order_again():
    rows = [
        _row("a", 1, local_path="videos/a.mp4"),
        _row("b", 2, local_path="videos/b.mp4"),
    ]
    video, wrapped = next_in_pass(rows, {"a", "b"})
    assert wrapped is True
    assert video["video_id"] == "a"


def test_room_for_extra_respects_the_cap_and_the_free_floor():
    cap = 0.75
    min_free = 1024 * 1024 * 1024
    assert room_for_extra(0.74, 20 * min_free, min_free, cap) is True
    assert room_for_extra(0.75, 20 * min_free, min_free, cap) is False
    assert room_for_extra(0.10, min_free - 1, min_free, cap) is False
    assert room_for_extra(0.50, 2 * min_free, min_free, cap, block_until_free=3 * min_free) is False
    assert room_for_extra(0.50, 3 * min_free, min_free, cap, block_until_free=3 * min_free) is True


def test_under_the_cap_keeps_the_floor_and_extras_on_disk():
    ordered = list("abcdefgh")
    existing = set("abcdefg")
    keep, drop = disk_window(ordered, 5, existing=existing, over_cap=False)
    assert drop == []
    assert keep == list("abcdefg")


def test_over_the_cap_drops_only_the_furthest_extra():
    ordered = list("abcdefgh")
    existing = set("abcdefg")
    keep, drop = disk_window(ordered, 5, on_air="a", existing=existing, over_cap=True)
    assert drop == ["g"]
    assert "a" not in drop
    assert keep == list("abcdef")
    # The on-air file is an extra at the end of the window. Drop the one before it.
    keep_air, drop_air = disk_window(
        ordered, 5, on_air="g", existing=existing, over_cap=True,
    )
    assert drop_air == ["f"]
    assert "g" not in drop_air
    assert keep_air == list("abcdeg")


def test_tail_scan_runs_once_at_the_last_five():
    assert tail_scan_due(5, 5, already=False) is True
    assert tail_scan_due(5, 5, already=True) is False
    assert tail_scan_due(6, 5, already=False) is False
