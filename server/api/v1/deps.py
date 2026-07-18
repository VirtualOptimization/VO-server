"""Shared FastAPI dependencies for API v1 endpoints."""

from __future__ import annotations

from fastapi import HTTPException, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from server.services.auth_service import get_user_by_access_token
from shared.db import SessionLocal
from shared.models.user import User

bearer_scheme = HTTPBearer(auto_error=False)


def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Security(bearer_scheme),
) -> User:
    token = credentials.credentials if credentials else ""
    if not token:
        raise HTTPException(status_code=401, detail="Authorization Bearer 토큰이 필요합니다.")

    db = SessionLocal()
    try:
        return get_user_by_access_token(db, token)
    finally:
        db.close()


def get_optional_current_user(
    credentials: HTTPAuthorizationCredentials | None = Security(bearer_scheme),
) -> User | None:
    token = credentials.credentials if credentials else ""
    if not token:
        return None

    db = SessionLocal()
    try:
        return get_user_by_access_token(db, token)
    finally:
        db.close()
