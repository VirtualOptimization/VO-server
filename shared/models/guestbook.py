"""Website guestbook entries, shown to every visitor."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Index, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from shared.db import Base


class GuestbookEntry(Base):
    __tablename__ = "guestbook_entries"
    __table_args__ = (Index("ix_guestbook_entries_created_at", "created_at"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    nickname: Mapped[str] = mapped_column(String(20), nullable=False)
    message: Mapped[str] = mapped_column(String(200), nullable=False)
    # S3 key of the uploaded photo; the API returns a presigned URL instead.
    photo_key: Mapped[str | None] = mapped_column(Text)
    consent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
