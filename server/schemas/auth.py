from __future__ import annotations

from pydantic import BaseModel, Field


class EmailSendCodeRequest(BaseModel):
    email: str = Field(..., min_length=5, max_length=255)


class EmailSendCodeResponse(BaseModel):
    message: str
    expires_in_seconds: int
    debug_verification_code: str | None = None


class EmailVerifyRequest(BaseModel):
    email: str = Field(..., min_length=5, max_length=255)
    code: str = Field(..., min_length=6, max_length=6)


class EmailVerifyResponse(BaseModel):
    message: str
    email: str
    verified: bool


class SignupRequest(BaseModel):
    nickname: str = Field(..., min_length=1, max_length=100)
    login_id: str = Field(..., min_length=4, max_length=100)
    password: str = Field(..., min_length=8, max_length=128)
    password_confirm: str = Field(..., min_length=8, max_length=128)
    email: str = Field(..., min_length=5, max_length=255)


class SignupResponse(BaseModel):
    user_id: int
    login_id: str
    email: str
    nickname: str
    is_email_verified: bool


class LoginRequest(BaseModel):
    login_id: str = Field(..., min_length=1, max_length=100)
    password: str = Field(..., min_length=1, max_length=128)


class AuthUserResponse(BaseModel):
    user_id: int
    login_id: str
    email: str
    nickname: str
    is_email_verified: bool


class LoginResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    user: AuthUserResponse


class RefreshTokenRequest(BaseModel):
    refresh_token: str = Field(..., min_length=32, max_length=512)


class RefreshTokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


class LogoutRequest(BaseModel):
    refresh_token: str = Field(..., min_length=32, max_length=512)


class LogoutResponse(BaseModel):
    message: str
