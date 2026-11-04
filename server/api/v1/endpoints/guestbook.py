"""Website guestbook: visitors leave a nickname, a message and an optional photo.

No login. Entries are public, so every visitor sees the same list. Photos are
stored in S3 and returned as presigned URLs.
"""
from __future__ import annotations

import asyncio
import logging
import re
import secrets
import uuid
from datetime import datetime, timedelta, timezone

import httpx
from fastapi import APIRouter, Header, HTTPException, Query
from fastapi.responses import RedirectResponse

from server.core.config import settings
from server.core.s3 import delete_objects, generate_presigned_url, object_exists, upload_bytes
from server.schemas.guestbook import (
    GuestbookCardRequest,
    GuestbookCardResponse,
    GuestbookCreateRequest,
    GuestbookEntryResponse,
    GuestbookListResponse,
)
from server.services.email_service import CardFile, send_guestbook_card
from server.services.guestbook_card_image import render_card
from server.services.guestbook_rules import (
    MESSAGE_MAX_LENGTH,
    NICKNAME_MAX_LENGTH,
    PHOTO_MAX_BYTES,
    InvalidInput,
    decode_card_image,
    decode_photo,
    detect_photo_type,
    normalize_email,
    normalize_mood,
    normalize_photo_url,
    normalize_text,
    require_consent,
)
from shared.db import SessionLocal
from shared.models.guestbook import GuestbookEntry

logger = logging.getLogger(__name__)
router = APIRouter()

PHOTO_PREFIX = "guestbook"
PHOTO_URL_EXPIRES_SECONDS = 24 * 60 * 60

# Card emails: the drawn card and its photo, kept so the email's save buttons keep working.
CARD_PREFIX = "guestbook-cards"
CARD_ID = re.compile(r"[0-9a-f]{32}")
CARD_FILE = re.compile(r"(card|photo)\.(jpg|png|webp|gif)")
CARD_URL_EXPIRES_SECONDS = 5 * 60
DOWNLOAD_NAMES = {"card": "V-O 방명록 카드", "photo": "V-O 방명록 사진"}
PHOTO_FETCH_TIMEOUT_SECONDS = 10
KST = timezone(timedelta(hours=9))


def _entry_response(entry: GuestbookEntry) -> GuestbookEntryResponse:
    return GuestbookEntryResponse(
        id=entry.id,
        nickname=entry.nickname,
        message=entry.message,
        mood=entry.mood,
        photo=generate_presigned_url(entry.photo_key, PHOTO_URL_EXPIRES_SECONDS) if entry.photo_key else None,
        created_at=entry.created_at,
    )


@router.get("", response_model=GuestbookListResponse, summary="방명록 목록 (최신순)")
async def list_guests(limit: int = Query(100, ge=1, le=500)):
    db = SessionLocal()
    try:
        entries = (
            db.query(GuestbookEntry)
            .order_by(GuestbookEntry.created_at.desc(), GuestbookEntry.id.desc())
            .limit(limit)
            .all()
        )
        return GuestbookListResponse(entries=[_entry_response(entry) for entry in entries])
    finally:
        db.close()


@router.post("/send-card", response_model=GuestbookCardResponse, summary="방명록 카드 메일 보내기 (웹사이트 서버 전용)")
async def send_card(payload: GuestbookCardRequest, x_guestbook_key: str | None = Header(default=None)):
    """Email a guestbook card to the address a visitor typed, using V-O's Gmail SMTP.

    The card (the website's capture, or drawn here like the website's card) and
    the photo are attached, and the email's save buttons download them from this server.
    Only the website server may call this: without the shared key anyone could
    send mail in V-O's name.
    """
    if not settings.guestbook_api_key:
        raise HTTPException(status_code=503, detail="카드 메일 발송이 설정되지 않았어요.")
    if not x_guestbook_key or not secrets.compare_digest(x_guestbook_key, settings.guestbook_api_key):
        raise HTTPException(status_code=401, detail="인증되지 않은 요청이에요.")
    try:
        to = normalize_email(payload.to)
        nickname = normalize_text(payload.nickname, field="닉네임", max_length=NICKNAME_MAX_LENGTH)
        message = normalize_text(payload.message, field="내용", max_length=MESSAGE_MAX_LENGTH)
        mood = normalize_mood(payload.mood)
        photo_url = normalize_photo_url(payload.photo_url)
        card_image = decode_card_image(payload.card_image)
    except InvalidInput as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None

    files = await _card_files(nickname, message, mood, photo_url, card_image)
    await asyncio.to_thread(send_guestbook_card, to, nickname, message, mood, photo_url, files)
    return GuestbookCardResponse(sent=True)


async def _fetch_photo(url: str) -> tuple[bytes, str, str] | None:
    """Download the card's photo from the website; None if it cannot be used."""
    try:
        async with httpx.AsyncClient(timeout=PHOTO_FETCH_TIMEOUT_SECONDS, follow_redirects=True) as client:
            async with client.stream("GET", url) as response:
                response.raise_for_status()
                data = bytearray()
                async for chunk in response.aiter_bytes():
                    data.extend(chunk)
                    if len(data) > PHOTO_MAX_BYTES:
                        logger.warning("방명록 카드 사진이 너무 커요 url=%s", url)
                        return None
    except httpx.HTTPError:
        logger.exception("방명록 카드 사진을 받지 못했어요 url=%s", url)
        return None
    photo_type = detect_photo_type(bytes(data))
    if photo_type is None:
        logger.warning("방명록 카드 사진 형식을 알 수 없어요 url=%s", url)
        return None
    return bytes(data), *photo_type


async def _card_files(
    nickname: str,
    message: str,
    mood: str | None,
    photo_url: str | None,
    card_image: tuple[bytes, str, str] | None = None,
) -> list[CardFile]:
    """The card image and the photo to attach, each with a save link when they could be stored.

    The website's capture is used as the card when it sent one. Otherwise the card
    is drawn here, but not when the photo cannot be downloaded: a card drawn
    without it would not match the card the visitor made.
    """
    photo = await _fetch_photo(photo_url) if photo_url else None
    if card_image is None:
        if photo_url and photo is None:
            return []
        written_on = datetime.now(KST).date()
        try:
            card_png = await asyncio.to_thread(render_card, nickname, message, mood, photo[0] if photo else None, written_on)
        except Exception:
            logger.exception("방명록 카드 이미지를 만들지 못했어요")
            return []
        card_image = (card_png, "image/png", "png")

    # (stored name, bytes, content type, button label)
    card_data, card_type, card_extension = card_image
    files = [(f"card.{card_extension}", card_data, card_type, "카드 저장하기")]
    if photo:
        data, content_type, extension = photo
        files.append((f"photo.{extension}", data, content_type, "사진 저장하기"))

    card_id = uuid.uuid4().hex
    stored = bool(settings.public_base_url)
    if stored:
        try:
            for name, data, content_type, _ in files:
                await upload_bytes(f"{CARD_PREFIX}/{card_id}/{name}", data, content_type)
        except Exception:
            logger.exception("방명록 카드 파일 업로드 실패 card_id=%s", card_id)
            stored = False
    base = settings.public_base_url.rstrip("/")
    return [
        CardFile(
            filename=_download_name(name),
            data=data,
            content_type=content_type,
            label=label,
            link=f"{base}/api/guests/cards/{card_id}/{name}" if stored else None,
        )
        for name, data, content_type, label in files
    ]


def _download_name(name: str) -> str:
    stem, extension = name.split(".")
    return f"{DOWNLOAD_NAMES[stem]}.{extension}"


@router.get("/cards/{card_id}/{name}", summary="방명록 카드 메일의 카드·사진 내려받기")
async def download_card_file(card_id: str, name: str):
    if not CARD_ID.fullmatch(card_id) or not CARD_FILE.fullmatch(name):
        raise HTTPException(status_code=404, detail="파일을 찾을 수 없어요.")
    key = f"{CARD_PREFIX}/{card_id}/{name}"
    if not await object_exists(key):
        raise HTTPException(status_code=404, detail="파일을 찾을 수 없어요.")
    url = generate_presigned_url(key, CARD_URL_EXPIRES_SECONDS, download_name=_download_name(name))
    return RedirectResponse(url, status_code=307)


@router.post("", response_model=GuestbookEntryResponse, status_code=201, summary="방명록 등록")
async def create_guest(payload: GuestbookCreateRequest):
    try:
        nickname = normalize_text(payload.nickname, field="닉네임", max_length=NICKNAME_MAX_LENGTH)
        message = normalize_text(payload.message, field="내용", max_length=MESSAGE_MAX_LENGTH)
        mood = normalize_mood(payload.mood)
        require_consent(payload.consent)
        photo = decode_photo(payload.photo)
    except InvalidInput as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None

    entry_id = uuid.uuid4().hex
    photo_key = None
    if photo is not None:
        data, content_type, extension = photo
        photo_key = f"{PHOTO_PREFIX}/{entry_id}.{extension}"
        try:
            await upload_bytes(photo_key, data, content_type)
        except Exception:
            logger.exception("방명록 사진 업로드 실패 entry_id=%s", entry_id)
            raise HTTPException(status_code=500, detail="사진을 저장하지 못했어요. 잠시 후 다시 시도해 주세요.") from None

    now = datetime.now(timezone.utc)
    db = SessionLocal()
    try:
        entry = GuestbookEntry(
            id=entry_id,
            nickname=nickname,
            message=message,
            mood=mood,
            photo_key=photo_key,
            consent_at=now,
            created_at=now,
        )
        db.add(entry)
        db.commit()
        db.refresh(entry)
        return _entry_response(entry)
    except Exception:
        db.rollback()
        logger.exception("방명록 저장 실패 entry_id=%s", entry_id)
        if photo_key:
            try:
                delete_objects([photo_key])
            except Exception:
                logger.exception("방명록 사진 정리 실패 key=%s", photo_key)
        raise HTTPException(status_code=500, detail="방명록을 저장하지 못했어요. 잠시 후 다시 시도해 주세요.") from None
    finally:
        db.close()
