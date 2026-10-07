from __future__ import annotations

import os

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials

security = HTTPBasic(auto_error=False)


def get_current_user(
    credentials: HTTPBasicCredentials | None = Depends(security),
) -> dict[str, str] | None:
    """Return the current authenticated user when dashboard auth is configured.

    In v1, auth is optional unless APP_USERNAME and APP_PASSWORD are set. This keeps
    local development simple while still enabling a basic password gate in production.
    """
    username = os.environ.get("APP_USERNAME")
    password = os.environ.get("APP_PASSWORD")

    if not username and not password:
        return {"username": "anonymous"}

    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
            headers={"WWW-Authenticate": "Basic realm=automatic-pdf-renamer"},
        )

    if credentials.username != username or credentials.password != password:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid username or password",
            headers={"WWW-Authenticate": "Basic realm=automatic-pdf-renamer"},
        )

    return {"username": credentials.username}
