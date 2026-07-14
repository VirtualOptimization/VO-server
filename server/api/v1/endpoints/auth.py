"""Authentication endpoints for signup email verification."""

from __future__ import annotations

from fastapi import APIRouter, Header, HTTPException

from server.core.security import create_access_token
from server.schemas.auth import (
    AuthUserResponse,
    EmailSendCodeRequest,
    EmailSendCodeResponse,
    EmailVerifyRequest,
    EmailVerifyResponse,
    LoginRequest,
    LoginResponse,
    LogoutRequest,
    LogoutResponse,
    RefreshTokenRequest,
    RefreshTokenResponse,
    SignupRequest,
    SignupResponse,
)
from server.services.auth_service import (
    authenticate_user,
    create_refresh_token,
    create_email_verification_code,
    create_user_after_email_verification,
    get_user_by_refresh_token,
    get_user_by_access_token,
    revoke_refresh_token,
    should_return_debug_code,
    verify_email_code,
)
from shared.models.user import User
from shared.db import SessionLocal

router = APIRouter()


def _to_auth_user_response(user: User) -> AuthUserResponse:
    return AuthUserResponse(
        user_id=user.id,
        login_id=user.login_id,
        email=user.email or "",
        nickname=user.nickname or "",
        is_email_verified=user.is_email_verified,
    )


@router.post("/email/send-code", response_model=EmailSendCodeResponse)
def send_email_verification_code(request: EmailSendCodeRequest):
    db = SessionLocal()
    try:
        code, expires_in_seconds = create_email_verification_code(db, request.email)
        return EmailSendCodeResponse(
            message="인증번호가 생성되었습니다.",
            expires_in_seconds=expires_in_seconds,
            debug_verification_code=code if should_return_debug_code() else None,
        )
    finally:
        db.close()


@router.post("/email/verify", response_model=EmailVerifyResponse)
def verify_email_verification_code(request: EmailVerifyRequest):
    db = SessionLocal()
    try:
        email = verify_email_code(db, request.email, request.code)
        return EmailVerifyResponse(
            message="이메일 인증이 완료되었습니다.",
            email=email,
            verified=True,
        )
    finally:
        db.close()


@router.post("/signup", response_model=SignupResponse)
def signup(request: SignupRequest):
    db = SessionLocal()
    try:
        user = create_user_after_email_verification(db, request)
        return SignupResponse(**_to_auth_user_response(user).model_dump())
    finally:
        db.close()


@router.post("/login", response_model=LoginResponse)
def login(request: LoginRequest):
    db = SessionLocal()
    try:
        user = authenticate_user(db, request.login_id, request.password)
        access_token = create_access_token(subject=str(user.id))
        refresh_token = create_refresh_token(db, user)
        return LoginResponse(
            access_token=access_token,
            refresh_token=refresh_token,
            user=_to_auth_user_response(user),
        )
    finally:
        db.close()


@router.post("/refresh", response_model=RefreshTokenResponse)
def refresh_access_token(request: RefreshTokenRequest):
    db = SessionLocal()
    try:
        user = get_user_by_refresh_token(db, request.refresh_token)
        return RefreshTokenResponse(access_token=create_access_token(subject=str(user.id)))
    finally:
        db.close()


@router.post("/logout", response_model=LogoutResponse)
def logout(request: LogoutRequest):
    db = SessionLocal()
    try:
        revoke_refresh_token(db, request.refresh_token)
        return LogoutResponse(message="로그아웃되었습니다.")
    finally:
        db.close()


@router.get("/me", response_model=AuthUserResponse)
def get_me(authorization: str = Header(default="")):
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Authorization Bearer 토큰이 필요합니다.")

    token = authorization.removeprefix("Bearer ").strip()
    db = SessionLocal()
    try:
        user = get_user_by_access_token(db, token)
        return _to_auth_user_response(user)
    finally:
        db.close()
