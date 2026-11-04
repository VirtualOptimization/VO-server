"""SMTP email delivery helpers."""

from __future__ import annotations

import logging
import smtplib
from collections.abc import Sequence
from dataclasses import dataclass
from email.message import EmailMessage
from html import escape

from fastapi import HTTPException

from server.core.config import settings

logger = logging.getLogger(__name__)


def is_smtp_configured() -> bool:
    return bool(settings.smtp_host and settings.smtp_username and settings.smtp_password)


def send_email_verification_code(email: str, code: str, expires_in_seconds: int) -> None:
    if not is_smtp_configured():
        if settings.env == "local" or settings.email_verification_debug:
            logger.info("SMTP is not configured. Verification code for %s: %s", email, code)
            return
        raise HTTPException(status_code=500, detail="이메일 발송 설정이 필요합니다.")

    from_email = settings.smtp_from_email or settings.smtp_username
    from_name = settings.smtp_from_name
    expires_minutes = max(1, expires_in_seconds // 60)

    message = EmailMessage()
    message["Subject"] = "[V-O] 이메일 인증번호 안내"
    message["From"] = f"{from_name} <{from_email}>"
    message["To"] = email
    message.set_content(
        "\n".join(
            [
                "V-O 이메일 인증번호 안내",
                "",
                f"인증번호: {code}",
                f"유효시간: {expires_minutes}분",
                "",
                "본인이 요청하지 않았다면 이 메일을 무시해주세요.",
            ]
        )
    )
    message.add_alternative(
        f"""
        <!doctype html>
        <html lang="ko">
          <body style="margin:0; padding:0; background:#f6f8fb; font-family:Arial, 'Apple SD Gothic Neo', 'Malgun Gothic', sans-serif; color:#1f2937;">
            <div style="max-width:520px; margin:0 auto; padding:32px 20px;">
              <div style="background:#ffffff; border:1px solid #e5e7eb; border-radius:16px; padding:32px 28px;">
                <p style="margin:0 0 12px; font-size:14px; font-weight:700; color:#9bb7e5;">V-O</p>
                <h1 style="margin:0 0 16px; font-size:24px; line-height:1.35; font-weight:800; color:#111827;">이메일 인증번호 안내</h1>
                <p style="margin:0 0 24px; font-size:15px; line-height:1.7; color:#4b5563;">회원가입을 완료하려면 아래 인증번호를 입력해주세요.</p>
                <div style="margin:0 0 24px; padding:20px 18px; border-radius:12px; background:#eef4ff; text-align:center;">
                  <div style="font-size:34px; line-height:1; font-weight:800; letter-spacing:8px; color:#3f6fb5;">{code}</div>
                </div>
                <p style="margin:0 0 8px; font-size:14px; line-height:1.6; color:#4b5563;">인증번호는 <strong style="font-weight:800; color:#111827;">{expires_minutes}분</strong> 동안 유효합니다.</p>
                <p style="margin:0; font-size:13px; line-height:1.6; color:#9ca3af;">본인이 요청하지 않았다면 이 메일을 무시해주세요.</p>
              </div>
            </div>
          </body>
        </html>
        """,
        subtype="html",
    )

    try:
        _send(message)
    except Exception:
        logger.exception("Failed to send verification email to %s", email)
        raise HTTPException(status_code=500, detail="인증번호 이메일 발송에 실패했습니다.")


def _send(message: EmailMessage) -> None:
    with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=10) as smtp:
        smtp.starttls()
        smtp.login(settings.smtp_username, settings.smtp_password)
        smtp.send_message(message)


@dataclass(frozen=True)
class CardFile:
    """A file attached to a guestbook card email, with its save button."""

    filename: str
    data: bytes
    content_type: str
    label: str
    # Where the button downloads it; without one the file is only attached.
    link: str | None = None


def send_guestbook_card(
    to: str,
    nickname: str,
    message_text: str,
    mood: str | None,
    photo_url: str | None,
    files: Sequence[CardFile] = (),
) -> None:
    """Email a guestbook card. Every visitor-written value is HTML-escaped."""
    if not is_smtp_configured():
        raise HTTPException(status_code=503, detail="메일 발송 설정이 필요합니다.")

    from_email = settings.smtp_from_email or settings.smtp_username
    name, body, feeling = escape(nickname), escape(message_text).replace("\n", "<br>"), escape(mood or "")
    photo_html = (
        f'<img src="{escape(photo_url)}" alt="" style="display:block; width:100%; border-radius:12px; margin:0 0 20px;">'
        if photo_url
        else ""
    )
    mood_html = f'<p style="margin:0 0 16px; font-size:13px; color:#3f6fb5;">{feeling}</p>' if mood else ""
    buttons = "".join(
        f'<a href="{escape(file.link)}" style="display:inline-block; margin:0 8px 8px 0; padding:11px 18px; border-radius:999px;'
        f' background:#7395c7; color:#ffffff; font-size:14px; font-weight:700; text-decoration:none;">{escape(file.label)}</a>'
        for file in files
        if file.link
    )
    buttons_html = f'<div style="margin:0 0 20px;">{buttons}</div>' if buttons else ""

    message = EmailMessage()
    # Headers cannot hold line breaks a visitor might type into the nickname.
    message["Subject"] = f"[V-O] {' '.join(nickname.split())}님이 방명록 카드를 보냈어요"
    message["From"] = f"{settings.smtp_from_name} <{from_email}>"
    message["To"] = to
    message.set_content(
        "\n".join(
            [f"{nickname}님이 V-O 방명록에 남긴 카드예요.", ""]
            + ([f"기분: {mood}"] if mood else [])
            + [message_text, "", "V-O 졸업 전시에서 보낸 메일입니다."]
        )
    )
    message.add_alternative(
        f"""
        <!doctype html>
        <html lang="ko">
          <body style="margin:0; padding:0; background:#f6f8fb; font-family:Arial, 'Apple SD Gothic Neo', 'Malgun Gothic', sans-serif; color:#1f2937;">
            <div style="max-width:520px; margin:0 auto; padding:32px 20px;">
              <div style="background:#ffffff; border:1px solid #e5e7eb; border-radius:16px; padding:32px 28px;">
                <p style="margin:0 0 12px; font-size:14px; font-weight:700; color:#9bb7e5;">V-O</p>
                <h1 style="margin:0 0 20px; font-size:22px; line-height:1.4; font-weight:800; color:#111827;">{name}님이 보낸 방명록 카드</h1>
                {photo_html}
                {mood_html}
                <p style="margin:0 0 24px; font-size:16px; line-height:1.7; color:#374151;">{body}</p>
                {buttons_html}
                <p style="margin:0; font-size:13px; line-height:1.6; color:#9ca3af;">V-O 졸업 전시에서 보낸 메일입니다.</p>
              </div>
            </div>
          </body>
        </html>
        """,
        subtype="html",
    )
    for file in files:
        maintype, subtype = file.content_type.split("/")
        message.add_attachment(file.data, maintype=maintype, subtype=subtype, filename=file.filename)

    try:
        _send(message)
    except Exception:
        logger.exception("Failed to send guestbook card to %s", to)
        raise HTTPException(status_code=502, detail="카드 메일을 보내지 못했어요. 잠시 후 다시 시도해 주세요.") from None
