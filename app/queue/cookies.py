"""YouTube cookies for yt-dlp. Downloads stay off until a live check passes."""
from __future__ import annotations

import json
import logging
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from app.config import settings

log = logging.getLogger(__name__)

_LOCK = threading.Lock()
_ok = False
_message = "Cookies do YouTube ainda não foram verificados."
_checked_at = 0.0
_source_mtime = -1.0
_netscape_path: Path | None = None
_pending_message = ""

_PROBE_URL = "https://www.youtube.com/watch?v=qvsZpbDnl2E"
_PLAYER_CLIENTS = ("android_sdkless", "web_embedded")
_player_client = ""
_EXPORT_TIMEOUT = 120
_AUTH_NAMES = frozenset({
    "LOGIN_INFO", "SID", "SAPISID",
    "__Secure-1PSID", "__Secure-3PSID",
})
_BOT_MARKERS = (
    "sign in to confirm",
    "not a bot",
    "cookies are no longer valid",
    "login required",
    "use --cookies",
    "http error 403",
    "confirm you’re not a bot",
    "confirm you're not a bot",
)


def status() -> dict:
    return {"ok": _ok, "message": _message, "checked_at": _checked_at}


def netscape_path() -> Path | None:
    return _netscape_path if _ok else None


def player_client() -> str:
    """YouTube client that passed the cookie probe. Empty until then."""
    return _player_client if _ok else ""


def mark_rejected(message: str) -> None:
    global _ok, _message, _checked_at
    _ok = False
    _message = message
    _checked_at = time.time()
    log.error("YouTube cookies rejected: %s", message)


def _reject(message: str, *, publish: bool) -> bool:
    global _pending_message
    _pending_message = message
    if publish:
        mark_rejected(message)
    return False


def invalidate() -> None:
    """Drop the cached verdict so the next sync re-runs the probe.

    A single video failing with 403 is not proof the cookies are dead. Only
    check_cookies decides that; this just makes the decision a fresh one.
    """
    global _checked_at
    _checked_at = 0.0


class BrowserCookiesError(Exception):
    """The browser cookie database could not be exported. Not the alarm by itself."""


class EdgeCookiesError(BrowserCookiesError):
    """Edge has no readable YouTube session. The page shows this; it is not the alarm."""


def cookie_file() -> Path:
    return settings.COOKIES_DIR / "cookiesyoutube.txt"


def store_netscape(text: str) -> None:
    """Write a Netscape cookie file. Anything else is refused and the current file stays."""
    if not _looks_netscape(text):
        raise ValueError(
            "O arquivo tem de estar no formato Netscape "
            "(# Netscape HTTP Cookie File, ou linhas separadas por tabulação)."
        )
    dest = cookie_file()
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(_normalize_netscape(text), encoding="utf-8")
    invalidate()


def _export_command(browser: str, dest: Path) -> list[str]:
    """Same flags as `yt-dlp --cookies-from-browser BROWSER --cookies FILE URL`."""
    return [
        sys.executable, "-m", "yt_dlp",
        "--cookies-from-browser", browser,
        "--cookies", str(dest),
        "--skip-download",
        "--quiet",
        "--no-warnings",
        _PROBE_URL,
    ]


def _restore_cookie_file(dest: Path, previous: str | None) -> None:
    if previous is None:
        if dest.is_file():
            dest.unlink()
        return
    dest.write_text(previous, encoding="utf-8")


def export_browser_cookies(browser: str, dest: Path | None = None) -> Path:
    """Run yt-dlp so it reads the browser and writes the Netscape file.

    A failed run puts the previous file back. A browser with no YouTube session
    does not replace a file that was already there.
    """
    dest = dest or cookie_file()
    dest.parent.mkdir(parents=True, exist_ok=True)
    previous = dest.read_text(encoding="utf-8") if dest.is_file() else None
    label = "Firefox" if browser == "firefox" else "Microsoft Edge"
    try:
        completed = subprocess.run(
            _export_command(browser, dest),
            capture_output=True,
            text=True,
            timeout=_EXPORT_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        _restore_cookie_file(dest, previous)
        log.warning("Cookie export from %s failed: %s", browser, exc)
        raise BrowserCookiesError(
            f"Não foi possível ler os cookies do {label}."
        ) from exc
    detail = (completed.stderr or completed.stdout or "").strip()
    has_login = dest.is_file() and _has_login(dest)
    # yt-dlp writes the Netscape file before it asks YouTube for the probe
    # video. A web-client "reload the page" answer is not a dead session.
    wrote_session = has_login and (completed.returncode == 0 or _page_reload(detail))
    if not wrote_session:
        _restore_cookie_file(dest, previous)
        log.warning("Cookie export from %s failed: %s", browser, detail[:400])
        if completed.returncode == 0:
            raise BrowserCookiesError(
                f"O {label} não tem sessão do YouTube. "
                "Entre no YouTube nesse navegador e tente de novo."
            )
        raise BrowserCookiesError(
            f"Não foi possível ler os cookies do {label}."
        )
    invalidate()
    return dest


def export_edge_cookies(dest: Path | None = None) -> Path:
    """Read Edge and store a Netscape file. Page errors stay EdgeCookiesError."""
    try:
        return export_browser_cookies("edge", dest)
    except BrowserCookiesError as exc:
        if isinstance(exc, EdgeCookiesError):
            raise
        raise EdgeCookiesError(str(exc)) from exc


def check_cookies(*, try_edge: bool = True) -> bool:
    """Convert the cookie file and ask yt-dlp for one video without downloading it.

    A failed file is not the alarm yet. Firefox is exported and the file is
    read again. Edge runs only when Firefox did not write a YouTube session.
    ok=false is published only after that last read fails.
    """
    if _accept_file(publish=not try_edge):
        return True
    if not try_edge:
        return False
    notes: list[str] = []
    file_note = _pending_message
    for browser in ("firefox", "edge"):
        try:
            export_browser_cookies(browser)
        except BrowserCookiesError as exc:
            notes.append(str(exc))
            continue
        if _accept_file(publish=False):
            return True
        notes.append(_pending_message)
        if browser == "firefox":
            break
    if _page_reload(_pending_message) or _page_reload(file_note):
        faults = " ".join(note for note in notes if note and not _page_reload(note))
        message = _reload_message()
        if faults:
            message = f"{message} {faults}"
        mark_rejected(message)
        return False
    detail = " ".join(part for part in (file_note, *notes) if part)
    mark_rejected(
        f"{detail} Os cookies lidos do Firefox e do Edge também foram recusados."
    )
    return False


def _accept_file(*, publish: bool = True) -> bool:
    """Validate the file on disk. Does not open a browser.

    publish=False remembers the reason and leaves the public ok flag alone,
    so the cookie alarm does not start in the middle of a browser export.
    """
    global _ok, _message, _checked_at, _source_mtime, _netscape_path, _player_client
    source = _find_source()
    if source is None:
        return _reject(
            "Nenhum arquivo de cookies em cookies/. Os vídeos não serão baixados.",
            publish=publish,
        )
    try:
        netscape = _to_netscape(source)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        log.info("Cookie file unreadable: %s", exc)
        return _reject(
            f"Arquivo de cookies ilegível ({source.name}). Os vídeos não serão baixados.",
            publish=publish,
        )
    if not _has_login(netscape):
        return _reject(
            "Os cookies não têm sessão do YouTube. Exporte de novo a pasta cookies. "
            "Os vídeos não serão baixados.",
            publish=publish,
        )
    try:
        _probe(netscape)
    except Exception as exc:
        text = str(exc)
        log.warning("Cookie probe failed: %s", text[:400])
        if _page_reload(text):
            reason = _reload_message()
        elif looks_like_cookie_failure(text):
            reason = (
                "O YouTube recusou os cookies no yt-dlp. Atualize o arquivo em cookies. "
                "Os vídeos não serão baixados."
            )
        else:
            reason = (
                "Não foi possível validar os cookies com o yt-dlp. "
                "Os vídeos não serão baixados."
            )
        return _reject(reason, publish=publish)
    with _LOCK:
        _ok = True
        _message = ""
        _checked_at = time.time()
        _source_mtime = source.stat().st_mtime
        _netscape_path = netscape
    log.info("YouTube cookies accepted from %s via %s", source.name, _player_client)
    return True


def ensure_fresh(max_age: float = 600) -> bool:
    """Reuse a recent result unless the cookie file changed."""
    source = _find_source()
    mtime = source.stat().st_mtime if source is not None else -1.0
    if _checked_at and time.time() - _checked_at < max_age and mtime == _source_mtime:
        return _ok
    return check_cookies()


def _find_source() -> Path | None:
    directory = settings.COOKIES_DIR
    if not directory.is_dir():
        return None
    for name in ("cookiesyoutube.txt", "cookiesyoutube.json"):
        preferred = directory / name
        if preferred.is_file():
            return preferred
    files = [
        path for path in directory.iterdir()
        if path.is_file() and path.suffix.lower() in {".json", ".txt"}
        and not path.name.startswith(".")
    ]
    if not files:
        return None
    return max(files, key=lambda path: path.stat().st_mtime)


def _to_netscape(source: Path) -> Path:
    text = source.read_text(encoding="utf-8")
    stripped = text.lstrip()
    if not stripped.startswith(("{", "[")):
        if not _looks_netscape(text):
            raise ValueError("not a netscape cookie file")
        dest = _netscape_dest()
        dest.write_text(_normalize_netscape(text), encoding="utf-8")
        return dest
    rows = json.loads(text)
    if not isinstance(rows, list) or not rows:
        raise ValueError("cookie json is empty")
    lines = ["# Netscape HTTP Cookie File", ""]
    for row in rows:
        if not isinstance(row, dict) or "name" not in row or "value" not in row:
            continue
        domain = str(row.get("domain") or "")
        if not domain:
            continue
        include_sub = "TRUE" if domain.startswith(".") else "FALSE"
        written_domain = f"#HttpOnly_{domain}" if row.get("httpOnly") else domain
        secure = "TRUE" if row.get("secure") else "FALSE"
        try:
            expires = str(int(float(row.get("expirationDate") or 0)))
        except (TypeError, ValueError):
            expires = "0"
        path = str(row.get("path") or "/")
        lines.append(
            "\t".join((written_domain, include_sub, path, secure, expires, str(row["name"]), str(row["value"])))
        )
    if len(lines) <= 2:
        raise ValueError("no cookie rows")
    dest = _netscape_dest()
    dest.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return dest


def _normalize_netscape(text: str) -> str:
    """Dot-leading domains must be TRUE or Python's cookiejar rejects the file."""
    lines = ["# Netscape HTTP Cookie File", ""]
    for line in text.splitlines():
        if not line.strip() or line.startswith("# Netscape") or line.startswith("# HTTP") or line.startswith("# Generated"):
            continue
        httponly = line.startswith("#HttpOnly_")
        raw = line[len("#HttpOnly_"):] if httponly else line
        if raw.startswith("#"):
            continue
        parts = raw.split("\t")
        if len(parts) < 7:
            continue
        domain = parts[0]
        parts[1] = "TRUE" if domain.startswith(".") else "FALSE"
        try:
            parts[4] = str(int(float(parts[4])))
        except (TypeError, ValueError):
            parts[4] = "0"
        rebuilt = "\t".join(parts)
        lines.append(("#HttpOnly_" + rebuilt) if httponly else rebuilt)
    if len(lines) <= 2:
        raise ValueError("no cookie rows")
    return "\n".join(lines) + "\n"


def _looks_netscape(text: str) -> bool:
    for line in text.splitlines():
        if line.startswith("#") or not line.strip():
            continue
        return line.count("\t") >= 6
    return False


def _netscape_dest() -> Path:
    return Path(tempfile.gettempdir()) / "newstube-youtube-cookies.txt"


def _has_login(path: Path) -> bool:
    names: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line or line.startswith("# Netscape") or line.startswith("# HTTP"):
            continue
        raw = line[len("#HttpOnly_"):] if line.startswith("#HttpOnly_") else line
        parts = raw.split("\t")
        if len(parts) >= 6:
            names.add(parts[5])
    return bool(names & _AUTH_NAMES)


class _SilentLogger:
    def debug(self, msg):
        return

    def info(self, msg):
        return

    def warning(self, msg):
        return

    def error(self, msg):
        return


def _has_video(info: dict | None) -> bool:
    if not info or not info.get("id"):
        return False
    for fmt in info.get("formats") or []:
        vcodec = fmt.get("vcodec")
        if vcodec and vcodec != "none":
            return True
    return False


def _probe(cookiefile: Path) -> None:
    """Ask YouTube for one video. The first client with a picture is kept.

    tv, web and mweb often return an id and no video formats, then yt-dlp
    says the format is not available. android_sdkless and web_embedded still
    list AVC1. A client without a picture is skipped.
    """
    global _player_client
    import yt_dlp

    errors: list[Exception] = []
    for client in _PLAYER_CLIENTS:
        opts = {
            "cookiefile": str(cookiefile),
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "noplaylist": True,
            "extract_flat": False,
            "ignore_no_formats_error": True,
            "logger": _SilentLogger(),
            "extractor_args": {"youtube": {"player_client": [client]}},
        }
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(_PROBE_URL, download=False)
        except Exception as exc:
            errors.append(exc)
            continue
        if _has_video(info):
            _player_client = client
            return
        errors.append(RuntimeError(f"{client} returned no video formats"))
    if errors:
        raise errors[-1]
    raise RuntimeError("yt-dlp returned no video")


def _page_reload(text: str) -> bool:
    return "the page needs to be reloaded" in text.lower() or "recarregar a página" in text.lower()


def _reload_message() -> str:
    return (
        "O YouTube pediu para recarregar a página ao validar os cookies. "
        "Os vídeos não serão baixados."
    )


def looks_like_cookie_failure(text: str) -> bool:
    folded = text.lower()
    return any(marker in folded for marker in _BOT_MARKERS)
