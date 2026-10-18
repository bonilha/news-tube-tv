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
    yt_cookies._player_client = ""
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


def test_export_uses_the_yt_dlp_command(monkeypatch, tmp_path):
    _reset(monkeypatch, tmp_path)
    seen = []

    def _run(args, **kwargs):
        seen.append(list(args))
        dest = args[args.index("--cookies") + 1]
        open(dest, "w", encoding="utf-8").write(_NETSCAPE)
        completed = type("Completed", (), {})()
        completed.returncode = 0
        completed.stderr = ""
        completed.stdout = ""
        return completed

    monkeypatch.setattr(yt_cookies.subprocess, "run", _run)
    yt_cookies.export_browser_cookies("firefox")
    command = seen[0]
    assert command[1:3] == ["-m", "yt_dlp"]
    assert command[command.index("--cookies-from-browser") + 1] == "firefox"
    assert command[command.index("--cookies") + 1].endswith("cookiesyoutube.txt")
    assert "--skip-download" in command
    assert command[-1] == "https://www.youtube.com/watch?v=qvsZpbDnl2E"
    assert "LOGIN_INFO" in yt_cookies.cookie_file().read_text(encoding="utf-8")


def test_reload_error_is_not_called_a_dead_session(monkeypatch, tmp_path):
    _reset(monkeypatch, tmp_path)
    yt_cookies.store_netscape(_NETSCAPE)

    def _probe(path):
        raise RuntimeError("ERROR: [youtube] qvsZpbDnl2E: The page needs to be reloaded.")

    def _export(browser, dest=None):
        raise yt_cookies.BrowserCookiesError(f"falha {browser}")

    monkeypatch.setattr(yt_cookies, "_probe", _probe)
    monkeypatch.setattr(yt_cookies, "export_browser_cookies", _export)
    assert yt_cookies.check_cookies() is False
    message = yt_cookies.status()["message"]
    assert "recarregar a página" in message
    assert "recusados" not in message
    assert "falha firefox" in message
    assert "falha edge" in message


def test_reload_keeps_the_session_file(monkeypatch, tmp_path):
    _reset(monkeypatch, tmp_path)

    def _run(args, **kwargs):
        dest = args[args.index("--cookies") + 1]
        open(dest, "w", encoding="utf-8").write(_NETSCAPE)
        completed = type("Completed", (), {})()
        completed.returncode = 1
        completed.stderr = "ERROR: [youtube] qvsZpbDnl2E: The page needs to be reloaded."
        completed.stdout = ""
        return completed

    monkeypatch.setattr(yt_cookies.subprocess, "run", _run)
    path = yt_cookies.export_browser_cookies("firefox")
    assert "LOGIN_INFO" in path.read_text(encoding="utf-8")


def test_dpapi_without_a_session_is_still_a_failure(monkeypatch, tmp_path):
    _reset(monkeypatch, tmp_path)

    def _run(args, **kwargs):
        completed = type("Completed", (), {})()
        completed.returncode = 1
        completed.stderr = "ERROR: Failed to decrypt with DPAPI."
        completed.stdout = ""
        return completed

    monkeypatch.setattr(yt_cookies.subprocess, "run", _run)
    try:
        yt_cookies.export_browser_cookies("edge")
    except yt_cookies.BrowserCookiesError as exc:
        assert "Microsoft Edge" in str(exc)
    else:
        raise AssertionError("expected BrowserCookiesError")
    assert not yt_cookies.cookie_file().is_file()


def test_firefox_session_does_not_open_edge(monkeypatch, tmp_path):
    _reset(monkeypatch, tmp_path)
    calls = []

    def _export(browser, dest=None):
        calls.append(browser)
        yt_cookies.store_netscape(_NETSCAPE)
        return yt_cookies.cookie_file()

    def _probe(path):
        raise RuntimeError("ERROR: [youtube] qvsZpbDnl2E: The page needs to be reloaded.")

    monkeypatch.setattr(yt_cookies, "export_browser_cookies", _export)
    monkeypatch.setattr(yt_cookies, "_probe", _probe)
    assert yt_cookies.check_cookies() is False
    assert calls == ["firefox"]
    assert "Microsoft Edge" not in yt_cookies.status()["message"]


def test_probe_accepts_the_second_player_client(monkeypatch, tmp_path):
    calls = []

    class FakeYDL:
        def __init__(self, opts):
            self.client = opts["extractor_args"]["youtube"]["player_client"][0]

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def extract_info(self, url, download=False):
            calls.append(self.client)
            if self.client == "android_sdkless":
                return {"id": "qvsZpbDnl2E", "formats": [{"vcodec": "none"}]}
            return {
                "id": "qvsZpbDnl2E",
                "formats": [{"vcodec": "avc1.4D401E", "height": 720}],
            }

    import yt_dlp
    monkeypatch.setattr(yt_dlp, "YoutubeDL", FakeYDL)
    yt_cookies._probe(tmp_path / "cookies.txt")
    assert calls == ["android_sdkless", "web_embedded"]
    assert yt_cookies._player_client == "web_embedded"


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
