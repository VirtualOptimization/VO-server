"""Website guestbook request/response schemas."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


class GuestbookCreateRequest(BaseModel):
    # Lengths, consent and the photo are checked in services/guestbook_rules.py so
    # the web gets one readable message instead of a validation error list.
    # A client-sent `created_at` is ignored; the server records the time.
    nickname: str | None = None
    message: str | None = None
    photo: str | None = None
    consent: bool | None = None


class GuestbookEntryResponse(BaseModel):
    id: str
    nickname: str
    message: str
    photo: str | None
    created_at: datetime


class GuestbookListResponse(BaseModel):
    entries: list[GuestbookEntryResponse]
