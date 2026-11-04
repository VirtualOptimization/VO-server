"""Website guestbook: visitors leave a nickname, a message and an optional photo.

No login. Entries are public, so every visitor sees the same list. Photos are
stored in S3 and returned as presigned URLs.
"""
from __future__ import annotations

import asyncio
import logging
import secrets
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Header, HTTPException, Query

from server.core.config import settings
from server.core.s3 import delete_objects, generate_presigned_url, upload_bytes
from server.schemas.guestbook import (
    GuestbookCardRequest,
    GuestbookCardResponse,
    GuestbookCreateRequest,
    GuestbookEntryResponse,
    GuestbookListResponse,
)
from server.services.email_service import send_guestbook_card
from server.services.guestbook_rules import (
    MESSAGE_MAX_LENGTH,
    MOOD_MAX_LENGTH,
    NICKNAME_MAX_LENGTH,
    InvalidInput,
    decode_photo,
    normalize_email,
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


def _entry_response(entry: GuestbookEntry) -> GuestbookEntryResponse:
    return GuestbookEntryResponse(
        id=entry.id,
        nickname=entry.nickname,
        message=entry.message,
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
        mood = (payload.mood or "").strip()[:MOOD_MAX_LENGTH] or None
        photo_url = normalize_photo_url(payload.photo_url)
    except InvalidInput as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None

    await asyncio.to_thread(send_guestbook_card, to, nickname, message, mood, photo_url)
    return GuestbookCardResponse(sent=True)


@router.post("", response_model=GuestbookEntryResponse, status_code=201, summary="방명록 등록")
async def create_guest(payload: GuestbookCreateRequest):
    try:
        nickname = normalize_text(payload.nickname, field="닉네임", max_length=NICKNAME_MAX_LENGTH)
        message = normalize_text(payload.message, field="내용", max_length=MESSAGE_MAX_LENGTH)
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
