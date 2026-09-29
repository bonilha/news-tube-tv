"""yt-dlp files and playable MP4s live in different folders."""

from app.queue.download import downloads_dir, queue_file


def test_playable_file_is_not_in_the_download_folder(monkeypatch, tmp_path):
    from app.config import settings
    monkeypatch.setattr(settings, "VIDEOS_DIR", tmp_path)
    ready = queue_file("abc")
    assert ready.parent == tmp_path / "queue"
    assert ready.parent != downloads_dir()
    assert downloads_dir() == tmp_path / "downloads"
