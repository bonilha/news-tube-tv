from __future__ import annotations

import hashlib
import hmac
import secrets

from fastapi import Depends, HTTPException, Request, status
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


def basic_ok(credentials: HTTPBasicCredentials | None) -> bool:
    if credentials is None:
        return False
    user_ok = secrets.compare_digest(credentials.username, settings.ADMIN_USER)
    pass_ok = secrets.compare_digest(credentials.password, settings.ADMIN_PASSWORD)
    return user_ok and pass_ok


async def verify_credentials(
    request: Request,
    credentials: HTTPBasicCredentials | None = Depends(_security),
) -> bool:
    if basic_ok(credentials):
        return True
    cookie = request.cookies.get(SESSION_COOKIE) or ""
    if cookie and secrets.compare_digest(cookie, session_token()):
        return True
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid credentials",
        headers={"WWW-Authenticate": "Basic"},
    )
