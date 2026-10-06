"""Members-only videos stay out of the queue and are not retried."""

import time

from app.queue.download import permanent_download_error
from app.yt.invidious import is_eligible, members_only, normalize_video

_MEMBERS = (
    "Join this channel to get access to members-only content like this video, "
    "and other exclusive perks."
)


def _public_payload(**extra) -> dict:
    now = int(time.time())
    payload = {
        "videoId": "abcdefghijk",
        "title": "Public",
        "lengthSeconds": 120,
        "published": now - 3600,
        "type": "video",
    }
    payload.update(extra)
    return payload


def test_detail_error_marks_members_only_and_blocks_eligibility():
    payload = _public_payload(error=_MEMBERS)
    assert members_only(payload) is True
    meta = normalize_video(payload)
    assert meta["is_members"] is True
    eligible, reason = is_eligible(meta, min_age_hours=0, now_unix=int(time.time()))
    assert eligible is False
    assert reason == "Conteúdo exclusivo para membros"


def test_public_detail_stays_eligible():
    payload = _public_payload()
    assert members_only(payload) is False
    meta = normalize_video(payload)
    eligible, reason = is_eligible(meta, min_age_hours=0, now_unix=int(time.time()))
    assert eligible is True
    assert reason == "ok"


def test_members_error_is_not_retried_and_a_generic_one_is():
    now = int(time.time())
    rows = [
        {
            "video_id": "members0001",
            "status": "pending",
            "published_unix": now,
            "duration": 60,
            "download_error": "Conteúdo exclusivo para membros",
        },
        {
            "video_id": "generic0001",
            "status": "pending",
            "published_unix": now,
            "duration": 60,
            "download_error": "yt-dlp did not produce an MP4",
        },
        {
            "video_id": "ytdlp000001",
            "status": "pending",
            "published_unix": now,
            "duration": 60,
            "download_error": _MEMBERS,
        },
    ]
    assert permanent_download_error(rows[0]["download_error"])
    assert permanent_download_error(rows[2]["download_error"])
    assert not permanent_download_error(rows[1]["download_error"])
    from app.queue.download import download_blocked

    retry = [
        row["video_id"] for row in rows
        if row.get("download_error")
        and not permanent_download_error(row["download_error"])
        and not download_blocked({**row, "download_error": ""})
    ]
    assert retry == ["generic0001"]
