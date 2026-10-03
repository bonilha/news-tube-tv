"""YouTube cookies for yt-dlp. Downloads stay off until a live check passes."""
from __future__ import annotations

import json
import logging
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

_PROBE_URL = "https://www.youtube.com/watch?v=jNQXAC9IVRw"
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


def mark_rejected(message: str) -> None:
    global _ok, _message, _checked_at
    _ok = False
    _message = message
    _checked_at = time.time()
    log.error("YouTube cookies rejected: %s", message)


def invalidate() -> None:
    """Drop the cached verdict so the next sync re-runs the probe.

    A single video failing with 403 is not proof the cookies are dead. Only
    check_cookies decides that; this just makes the decision a fresh one.
    """
    global _checked_at
    _checked_at = 0.0


class EdgeCookiesError(Exception):
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


def export_edge_cookies(dest: Path | None = None) -> Path:
    """Read the Default Edge profile and store a Netscape file."""
    dest = dest or cookie_file()
    try:
        from yt_dlp.cookies import extract_cookies_from_browser
        jar = extract_cookies_from_browser("edge", "Default")
    except Exception as exc:
        raise EdgeCookiesError(
            "Não foi possível ler os cookies do Microsoft Edge."
        ) from exc
    lines = ["# Netscape HTTP Cookie File", ""]
    names: set[str] = set()
    for cookie in jar:
        domain = cookie.domain or ""
        if "youtube.com" not in domain and "google.com" not in domain:
            continue
        include_sub = "TRUE" if domain.startswith(".") else "FALSE"
        secure = "TRUE" if cookie.secure else "FALSE"
        expires = str(int(cookie.expires or 0))
        prefix = "#HttpOnly_" if cookie.has_nonstandard_attr("HttpOnly") else ""
        lines.append("\t".join((
            f"{prefix}{domain}", include_sub, cookie.path or "/",
            secure, expires, cookie.name, cookie.value or "",
        )))
        names.add(cookie.name)
    if not (names & _AUTH_NAMES):
        raise EdgeCookiesError(
            "O Edge não tem sessão do YouTube. Entre no YouTube nesse navegador e tente de novo."
        )
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text("\n".join(lines) + "\n", encoding="utf-8")
    invalidate()
    return dest


def check_cookies(*, try_edge: bool = True) -> bool:
    """Convert the cookie file and ask yt-dlp for one video without downloading it.

    One failed check reads Edge once and checks again. The alarm stays off until
    that second check also fails.
    """
    if _accept_file():
        return True
    if not try_edge:
        return False
    first = _message
    try:
        export_edge_cookies()
    except EdgeCookiesError as exc:
        mark_rejected(f"{first} Leitura automática do Edge também falhou: {exc}")
        return False
    if _accept_file():
        return True
    mark_rejected(
        f"{_message} Os cookies lidos do Edge também foram recusados."
    )
    return False


def _accept_file() -> bool:
    """Validate the file on disk. Does not call Edge."""
    global _ok, _message, _checked_at, _source_mtime, _netscape_path
    source = _find_source()
    if source is None:
        mark_rejected("Nenhum arquivo de cookies em cookies/. Os vídeos não serão baixados.")
        return False
    try:
        netscape = _to_netscape(source)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        mark_rejected(f"Arquivo de cookies ilegível ({source.name}). Os vídeos não serão baixados.")
        log.info("Cookie file unreadable: %s", exc)
        return False
    if not _has_login(netscape):
        mark_rejected(
            "Os cookies não têm sessão do YouTube. Exporte de novo a pasta cookies. "
            "Os vídeos não serão baixados."
        )
        return False
    try:
        _probe(netscape)
    except Exception as exc:
        text = str(exc)
        if looks_like_cookie_failure(text):
            mark_rejected(
                "O YouTube recusou os cookies no yt-dlp. Atualize o arquivo em cookies. "
                "Os vídeos não serão baixados."
            )
        else:
            mark_rejected(
                "Não foi possível validar os cookies com o yt-dlp. "
                "Os vídeos não serão baixados."
            )
        log.warning("Cookie probe failed: %s", text[:400])
        return False
    with _LOCK:
        _ok = True
        _message = ""
        _checked_at = time.time()
        _source_mtime = source.stat().st_mtime
        _netscape_path = netscape
    log.info("YouTube cookies accepted from %s", source.name)
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


def _probe(cookiefile: Path) -> None:
    import yt_dlp

    opts = {
        "cookiefile": str(cookiefile),
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
        "extract_flat": False,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(_PROBE_URL, download=False)
    if not info or not info.get("id"):
        raise RuntimeError("yt-dlp returned no video")


def looks_like_cookie_failure(text: str) -> bool:
    folded = text.lower()
    return any(marker in folded for marker in _BOT_MARKERS)
