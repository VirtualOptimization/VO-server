"""Authentication endpoints for signup email verification."""

from __future__ import annotations

from fastapi import APIRouter

from server.schemas.auth import (
    EmailSendCodeRequest,
    EmailSendCodeResponse,
    EmailVerifyRequest,
    EmailVerifyResponse,
    SignupRequest,
    SignupResponse,
)
from server.services.auth_service import (
    create_email_verification_code,
    create_user_after_email_verification,
    should_return_debug_code,
    verify_email_code,
)
from shared.db import SessionLocal

router = APIRouter()


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
        return SignupResponse(
            user_id=user.id,
            login_id=user.login_id,
            email=user.email or "",
            nickname=user.nickname or "",
            is_email_verified=user.is_email_verified,
        )
    finally:
        db.close()
