"""Guestbook input rules that do not touch the database or S3."""

from __future__ import annotations

import base64
import binascii
import re

NICKNAME_MAX_LENGTH = 20
MESSAGE_MAX_LENGTH = 200
PHOTO_MAX_BYTES = 5 * 1024 * 1024

# content type -> (file extension, magic-byte check)
PHOTO_TYPES = {
    "image/jpeg": ("jpg", lambda data: data.startswith(b"\xff\xd8\xff")),
    "image/png": ("png", lambda data: data.startswith(b"\x89PNG\r\n\x1a\n")),
    "image/webp": ("webp", lambda data: data[:4] == b"RIFF" and data[8:12] == b"WEBP"),
    "image/gif": ("gif", lambda data: data[:6] in (b"GIF87a", b"GIF89a")),
}
_DATA_URL = re.compile(r"^data:(?P<type>[\w.+-]+/[\w.+-]+);base64,(?P<data>.*)$", re.DOTALL)


class InvalidInput(ValueError):
    """Input a visitor can fix; the message is shown to them as is."""


def normalize_text(value: str | None, *, field: str, max_length: int) -> str:
    """`field` is "닉네임" or "내용" (both take 을/은)."""
    text = (value or "").strip()
    if not text:
        raise InvalidInput(f"{field}을 입력해 주세요.")
    if len(text) > max_length:
        raise InvalidInput(f"{field}은 {max_length}자까지 쓸 수 있어요.")
    return text


def require_consent(consent: bool | None) -> None:
    if consent is not True:
        raise InvalidInput("개인정보 수집·이용에 동의해 주세요.")


def decode_photo(photo: str | None) -> tuple[bytes, str, str] | None:
    """Decode a `data:image/...;base64,` URL into (bytes, content type, extension)."""
    if photo is None or photo == "":
        return None

    match = _DATA_URL.match(photo)
    if match is None:
        raise InvalidInput("사진 형식이 올바르지 않아요.")
    content_type = match["type"].lower()
    if content_type not in PHOTO_TYPES:
        raise InvalidInput("사진은 JPG, PNG, WEBP, GIF만 올릴 수 있어요.")

    encoded = match["data"]
    if len(encoded) > PHOTO_MAX_BYTES * 4 // 3 + 4:
        raise InvalidInput("사진은 5MB 이하만 올릴 수 있어요.")
    try:
        data = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError):
        raise InvalidInput("사진 형식이 올바르지 않아요.") from None
    if len(data) > PHOTO_MAX_BYTES:
        raise InvalidInput("사진은 5MB 이하만 올릴 수 있어요.")

    extension, looks_valid = PHOTO_TYPES[content_type]
    if not looks_valid(data):
        raise InvalidInput("사진 파일이 올바르지 않아요.")
    return data, content_type, extension
