"""Netscape upload and browser export before the cookie alarm."""

from app.queue import cookies as yt_cookies

_NETSCAPE = (
    "# Netscape HTTP Cookie File\n"
    ".youtube.com\tTRUE\t/\tTRUE\t2000000000\tLOGIN_INFO\tabc\n"
)


def _reset(monkeypatch, tmp_path):
    monkeypatch.setattr(yt_cookies.settings, "COOKIES_DIR", tmp_path)
    yt_cookies._ok = False
    yt_cookies._message = ""
    yt_cookies._pending_message = ""
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


def test_failed_file_reads_firefox_and_stops(monkeypatch, tmp_path):
    _reset(monkeypatch, tmp_path)
    calls = []

    def _export(browser, dest=None):
        calls.append(browser)
        yt_cookies.store_netscape(_NETSCAPE)
        return yt_cookies.cookie_file()

    monkeypatch.setattr(yt_cookies, "export_browser_cookies", _export)
    assert yt_cookies.check_cookies() is True
    assert calls == ["firefox"]
    assert yt_cookies.check_cookies() is True
    assert calls == ["firefox"]


def test_edge_is_used_when_firefox_fails(monkeypatch, tmp_path):
    _reset(monkeypatch, tmp_path)
    calls = []

    def _export(browser, dest=None):
        calls.append(browser)
        if browser == "firefox":
            raise yt_cookies.BrowserCookiesError("Não foi possível ler os cookies do Firefox.")
        yt_cookies.store_netscape(_NETSCAPE)
        return yt_cookies.cookie_file()

    monkeypatch.setattr(yt_cookies, "export_browser_cookies", _export)
    assert yt_cookies.check_cookies() is True
    assert calls == ["firefox", "edge"]


def test_alarm_waits_until_both_browsers_and_the_reread_fail(monkeypatch, tmp_path):
    _reset(monkeypatch, tmp_path)
    published = []
    real = yt_cookies.mark_rejected

    def _spy(message):
        published.append(message)
        real(message)

    def _export(browser, dest=None):
        raise yt_cookies.BrowserCookiesError(f"falha {browser}")

    monkeypatch.setattr(yt_cookies, "mark_rejected", _spy)
    monkeypatch.setattr(yt_cookies, "export_browser_cookies", _export)
    assert yt_cookies.check_cookies() is False
    assert len(published) == 1
    assert "falha firefox" in published[0]
    assert "falha edge" in published[0]
    assert yt_cookies.status()["ok"] is False


def test_check_without_retry_does_not_open_a_browser(monkeypatch, tmp_path):
    _reset(monkeypatch, tmp_path)
    calls = []
    monkeypatch.setattr(
        yt_cookies,
        "export_browser_cookies",
        lambda browser, dest=None: calls.append(browser),
    )
    assert yt_cookies.check_cookies(try_edge=False) is False
    assert calls == []
    assert yt_cookies.status()["ok"] is False


def test_edge_button_error_stays_a_page_error(monkeypatch, tmp_path):
    _reset(monkeypatch, tmp_path)

    def _export(browser, dest=None):
        raise yt_cookies.BrowserCookiesError("Não foi possível ler os cookies do Microsoft Edge.")

    monkeypatch.setattr(yt_cookies, "export_browser_cookies", _export)
    try:
        yt_cookies.export_edge_cookies()
    except yt_cookies.EdgeCookiesError as exc:
        assert "Microsoft Edge" in str(exc)
    else:
        raise AssertionError("expected EdgeCookiesError")
    assert yt_cookies.status()["ok"] is False
    assert yt_cookies.status()["message"] == ""
