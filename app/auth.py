from __future__ import annotations

import hashlib
import hmac
import secrets

import base64

from fastapi import Depends, HTTPException, Request, Response, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from app.config import settings

SESSION_COOKIE = "nt_session"
_security = HTTPBasic(auto_error=False)


def session_token() -> str:
    return hmac.new(
        settings.ADMIN_PASSWORD.encode(),
        b"newstube-session:" + settings.ADMIN_USER.encode(),
        hashlib.sha256,
    ).hexdigest()


def credentials_from_header(header: str) -> HTTPBasicCredentials | None:
    """Basic header only. A missing or broken header is anonymous, not an error."""
    scheme, _, encoded = (header or "").partition(" ")
    if scheme.lower() != "basic" or not encoded:
        return None
    try:
        decoded = base64.b64decode(encoded).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return None
    user, sep, password = decoded.partition(":")
    if not sep:
        return None
    return HTTPBasicCredentials(username=user, password=password)


def basic_ok(credentials: HTTPBasicCredentials | None) -> bool:
    if credentials is None:
        return False
    user_ok = secrets.compare_digest(credentials.username, settings.ADMIN_USER)
    pass_ok = secrets.compare_digest(credentials.password, settings.ADMIN_PASSWORD)
    return user_ok and pass_ok


async def verify_credentials(
    request: Request,
    response: Response,
    credentials: HTTPBasicCredentials | None = Depends(_security),
) -> bool:
    """Accept Basic or the session cookie. Basic is what stores the cookie.

    The cookie is set here, on the route response, so polls do not pass
    through BaseHTTPMiddleware. That middleware opens a task group per
    request and, across a multi-day HTMX poll, left tasks behind until the
    process stopped answering.
    """
    if basic_ok(credentials):
        response.set_cookie(
            SESSION_COOKIE,
            session_token(),
            httponly=True,
            samesite="lax",
            path="/",
        )
        return True
    cookie = request.cookies.get(SESSION_COOKIE) or ""
    if cookie and secrets.compare_digest(cookie, session_token()):
        return True
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid credentials",
        headers={"WWW-Authenticate": "Basic"},
    )


class SessionCookie:
    """Set nt_session on a successful Basic login without BaseHTTPMiddleware.

    Starlette's @middleware("http") opens a task group per request. Days of
    HTMX polls left those tasks behind until the loop stopped serving.
    This wrapper only looks at the start line of the response.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        header = ""
        for key, value in scope.get("headers") or []:
            if key == b"authorization":
                header = value.decode("latin-1")
                break
        remember = basic_ok(credentials_from_header(header))

        async def send_with_cookie(message):
            if (
                remember
                and message["type"] == "http.response.start"
                and int(message.get("status") or 0) < 400
            ):
                headers = list(message.get("headers") or [])
                cookie = (
                    f"{SESSION_COOKIE}={session_token()}; "
                    "HttpOnly; Path=/; SameSite=lax"
                )
                headers.append((b"set-cookie", cookie.encode("latin-1")))
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_with_cookie)
