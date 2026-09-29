"""yt-dlp files and playable MP4s live in different folders."""

from app.queue.download import downloads_dir, queue_file, raw_download


def test_playable_file_is_not_in_the_download_folder(monkeypatch, tmp_path):
    from app.config import settings
    monkeypatch.setattr(settings, "VIDEOS_DIR", tmp_path)
    ready = queue_file("abc")
    assert ready.parent == tmp_path / "queue"
    assert ready.parent != downloads_dir()
    assert downloads_dir() == tmp_path / "downloads"


def test_raw_download_is_the_merged_file_not_a_fragment(monkeypatch, tmp_path):
    from app.config import settings
    monkeypatch.setattr(settings, "VIDEOS_DIR", tmp_path)
    folder = downloads_dir()
    folder.mkdir(parents=True)
    (folder / "abc.f299.mp4").write_bytes(b"x")
    (folder / "abc.mp4.part").write_bytes(b"y")
    assert raw_download("abc") is None
    merged = folder / "abc.mp4"
    merged.write_bytes(b"z")
    assert raw_download("abc") == merged
