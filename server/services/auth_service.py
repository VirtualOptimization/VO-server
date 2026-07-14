"""Authentication service logic for signup and email verification."""

from __future__ import annotations

import logging
import hashlib
import re
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException
from sqlalchemy.orm import Session

from server.core.config import settings
from server.core.security import decode_access_token, hash_secret, verify_secret
from server.schemas.auth import SignupRequest
from shared.models.email_verification_code import EmailVerificationCode
from shared.models.refresh_token import RefreshToken
from shared.models.user import User

logger = logging.getLogger(__name__)

EMAIL_CODE_EXPIRE_SECONDS = 10 * 60
REFRESH_TOKEN_EXPIRE_DAYS = 30
EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _hash_refresh_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def normalize_email(email: str) -> str:
    return email.strip().lower()


def validate_email(email: str) -> str:
    normalized = normalize_email(email)
    if not EMAIL_PATTERN.match(normalized):
        raise HTTPException(status_code=400, detail="올바른 이메일 형식이 아닙니다.")
    return normalized


def validate_password(password: str, password_confirm: str) -> None:
    if password != password_confirm:
        raise HTTPException(status_code=400, detail="비밀번호가 일치하지 않습니다.")
    if len(password) < 8:
        raise HTTPException(status_code=400, detail="비밀번호는 8자 이상이어야 합니다.")
    if not re.search(r"[A-Z]", password) and not re.search(r"[^A-Za-z0-9]", password):
        raise HTTPException(status_code=400, detail="비밀번호는 대문자 또는 특수기호를 포함해야 합니다.")


def create_email_verification_code(db: Session, email: str) -> tuple[str, int]:
    normalized_email = validate_email(email)

    existing_user = db.query(User).filter(User.email == normalized_email).first()
    if existing_user is not None:
        raise HTTPException(status_code=409, detail="이미 가입된 이메일입니다.")

    code = f"{secrets.randbelow(1_000_000):06d}"
    expires_at = _now() + timedelta(seconds=EMAIL_CODE_EXPIRE_SECONDS)
    verification = EmailVerificationCode(
        email=normalized_email,
        code_hash=hash_secret(code),
        purpose="SIGNUP",
        expires_at=expires_at,
    )
    db.add(verification)
    db.commit()

    # Local placeholder for future SMTP/SES integration.
    logger.info("Email verification code created for %s: %s", normalized_email, code)
    return code, EMAIL_CODE_EXPIRE_SECONDS


def verify_email_code(db: Session, email: str, code: str) -> str:
    normalized_email = validate_email(email)
    verification = (
        db.query(EmailVerificationCode)
        .filter(
            EmailVerificationCode.email == normalized_email,
            EmailVerificationCode.purpose == "SIGNUP",
            EmailVerificationCode.verified_at.is_(None),
        )
        .order_by(EmailVerificationCode.created_at.desc(), EmailVerificationCode.id.desc())
        .first()
    )
    if verification is None:
        raise HTTPException(status_code=404, detail="인증번호 요청 내역이 없습니다.")
    if verification.expires_at < _now():
        raise HTTPException(status_code=400, detail="인증번호가 만료되었습니다.")
    if not verify_secret(code, verification.code_hash):
        raise HTTPException(status_code=400, detail="인증번호가 일치하지 않습니다.")

    verification.verified_at = _now()
    db.commit()
    return normalized_email


def create_user_after_email_verification(db: Session, request: SignupRequest) -> User:
    normalized_email = validate_email(request.email)
    login_id = request.login_id.strip()
    nickname = request.nickname.strip()
    if not login_id:
        raise HTTPException(status_code=400, detail="아이디를 입력해주세요.")
    if not nickname:
        raise HTTPException(status_code=400, detail="이름을 입력해주세요.")

    validate_password(request.password, request.password_confirm)

    if db.query(User).filter(User.login_id == login_id).first() is not None:
        raise HTTPException(status_code=409, detail="이미 사용 중인 아이디입니다.")
    if db.query(User).filter(User.email == normalized_email).first() is not None:
        raise HTTPException(status_code=409, detail="이미 가입된 이메일입니다.")

    verification = (
        db.query(EmailVerificationCode)
        .filter(
            EmailVerificationCode.email == normalized_email,
            EmailVerificationCode.purpose == "SIGNUP",
            EmailVerificationCode.verified_at.is_not(None),
        )
        .order_by(EmailVerificationCode.verified_at.desc(), EmailVerificationCode.id.desc())
        .first()
    )
    if verification is None:
        raise HTTPException(status_code=400, detail="이메일 인증이 필요합니다.")
    if verification.expires_at < _now():
        raise HTTPException(status_code=400, detail="이메일 인증이 만료되었습니다.")

    user = User(
        login_id=login_id,
        password_hash=hash_secret(request.password),
        email=normalized_email,
        nickname=nickname,
        is_email_verified=True,
    )
    db.add(user)
    db.flush()
    verification.user_id = user.id
    db.commit()
    db.refresh(user)
    return user


def should_return_debug_code() -> bool:
    return settings.env == "local"


def authenticate_user(db: Session, login_id: str, password: str) -> User:
    user = db.query(User).filter(User.login_id == login_id.strip()).first()
    if user is None or not verify_secret(password, user.password_hash):
        raise HTTPException(status_code=401, detail="아이디 또는 비밀번호가 일치하지 않습니다.")
    return user


def create_refresh_token(db: Session, user: User) -> str:
    token = secrets.token_urlsafe(48)
    refresh_token = RefreshToken(
        user_id=user.id,
        token_hash=_hash_refresh_token(token),
        expires_at=_now() + timedelta(days=REFRESH_TOKEN_EXPIRE_DAYS),
    )
    db.add(refresh_token)
    db.commit()
    return token


def get_user_by_refresh_token(db: Session, token: str) -> User:
    refresh_token = (
        db.query(RefreshToken)
        .filter(RefreshToken.token_hash == _hash_refresh_token(token))
        .first()
    )
    if refresh_token is None:
        raise HTTPException(status_code=401, detail="유효하지 않은 refresh token입니다.")
    if refresh_token.revoked_at is not None:
        raise HTTPException(status_code=401, detail="폐기된 refresh token입니다.")
    if refresh_token.expires_at < _now():
        raise HTTPException(status_code=401, detail="만료된 refresh token입니다.")

    return refresh_token.user


def revoke_refresh_token(db: Session, token: str) -> None:
    refresh_token = (
        db.query(RefreshToken)
        .filter(RefreshToken.token_hash == _hash_refresh_token(token))
        .first()
    )
    if refresh_token is None:
        raise HTTPException(status_code=401, detail="유효하지 않은 refresh token입니다.")

    refresh_token.revoked_at = _now()
    db.commit()


def get_user_by_access_token(db: Session, token: str) -> User:
    payload = decode_access_token(token)
    subject = payload.get("sub")
    if not isinstance(subject, str) or not subject.isdigit():
        raise HTTPException(status_code=401, detail="유효하지 않은 인증 토큰입니다.")

    user = db.get(User, int(subject))
    if user is None:
        raise HTTPException(status_code=401, detail="사용자를 찾을 수 없습니다.")
    return user
