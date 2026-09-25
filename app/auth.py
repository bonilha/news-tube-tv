from __future__ import annotations

import secrets
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from app.config import settings

_security = HTTPBasic()


async def verify_credentials(
    credentials: HTTPBasicCredentials = Depends(_security),
) -> bool:
    correct_user = secrets.compare_digest(
        credentials.username, settings.ADMIN_USER
    )
    correct_pass = secrets.compare_digest(
        credentials.password, settings.ADMIN_PASSWORD
    )
    if not (correct_user and correct_pass):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": "Basic"},
        )
    return True
