"""Netscape upload and the single Edge retry before the cookie alarm."""

from app.queue import cookies as yt_cookies

_NETSCAPE = (
    "# Netscape HTTP Cookie File\n"
    ".youtube.com\tTRUE\t/\tTRUE\t2000000000\tLOGIN_INFO\tabc\n"
)


def _reset(monkeypatch, tmp_path):
    monkeypatch.setattr(yt_cookies.settings, "COOKIES_DIR", tmp_path)
    yt_cookies._ok = False
    yt_cookies._message = ""
    yt_cookies._checked_at = 0.0
    yt_cookies._source_mtime = -1.0
    yt_cookies._netscape_path = None
    monkeypatch.setattr(yt_cookies, "_probe", lambda path: None)


def test_netscape_file_is_stored(monkeypatch, tmp_path):
    _reset(monkeypatch, tmp_path)
    yt_cookies.store_netscape(_NETSCAPE)
    assert yt_cookies.check_cookies(try_edge=False) is True
    assert "LOGIN_INFO" in yt_cookies.cookie_file().read_text(encoding="utf-8")


def test_other_text_is_refused_and_the_file_stays(monkeypatch, tmp_path):
    _reset(monkeypatch, tmp_path)
    yt_cookies.store_netscape(_NETSCAPE)
    before = yt_cookies.cookie_file().read_text(encoding="utf-8")
    try:
        yt_cookies.store_netscape("isto nao e netscape")
    except ValueError as exc:
        assert "Netscape" in str(exc)
    else:
        raise AssertionError("expected ValueError")
    assert yt_cookies.cookie_file().read_text(encoding="utf-8") == before


def test_failed_check_reads_edge_once(monkeypatch, tmp_path):
    _reset(monkeypatch, tmp_path)
    calls = {"n": 0}

    def _edge():
        calls["n"] += 1
        yt_cookies.store_netscape(_NETSCAPE)
        return yt_cookies.cookie_file()

    monkeypatch.setattr(yt_cookies, "export_edge_cookies", _edge)
    assert yt_cookies.check_cookies() is True
    assert calls["n"] == 1
    assert yt_cookies.check_cookies() is True
    assert calls["n"] == 1


def test_edge_failure_is_the_rejection(monkeypatch, tmp_path):
    _reset(monkeypatch, tmp_path)

    def _edge():
        raise yt_cookies.EdgeCookiesError("Edge fechado")

    monkeypatch.setattr(yt_cookies, "export_edge_cookies", _edge)
    assert yt_cookies.check_cookies() is False
    message = yt_cookies.status()["message"]
    assert "Edge fechado" in message
    assert yt_cookies.status()["ok"] is False
