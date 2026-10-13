"""Exhibition challenge ORM models: an event, its participants and their attempts."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from shared.db import Base


class Event(Base):
    """A challenge on one room: visitors start from `start_version` and are scored
    against the AI layout in `target_version` (see workers/optimizer/scoring.py)."""

    __tablename__ = "events"

    id: Mapped[int] = mapped_column(primary_key=True)
    owner_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    room_id: Mapped[int] = mapped_column(ForeignKey("rooms.id", ondelete="CASCADE"), nullable=False)
    start_version_id: Mapped[int] = mapped_column(ForeignKey("versions.id", ondelete="CASCADE"), nullable=False)
    target_version_id: Mapped[int] = mapped_column(ForeignKey("versions.id", ondelete="CASCADE"), nullable=False)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    # Score of the AI layout, the reference for the space part (computed at creation).
    ai_record: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    participants: Mapped[list["EventParticipant"]] = relationship(back_populates="event", cascade="all, delete-orphan")


class EventParticipant(Base):
    """A visitor registered for an event. Authenticates with a per-participant token."""

    __tablename__ = "event_participants"
    __table_args__ = (UniqueConstraint("event_id", "phone", name="uq_event_participants_event_phone"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id", ondelete="CASCADE"), nullable=False)
    name: Mapped[str] = mapped_column(String(30), nullable=False)
    # Contact for prizes only; never returned by any API.
    phone: Mapped[str] = mapped_column(String(20), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    privacy_consent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())

    event: Mapped[Event] = relationship(back_populates="participants")
    attempts: Mapped[list["EventAttempt"]] = relationship(back_populates="participant", cascade="all, delete-orphan")


class EventAttempt(Base):
    """One scored submission. `idempotency_key` makes client retries return the same result."""

    __tablename__ = "event_attempts"
    __table_args__ = (
        UniqueConstraint("participant_id", "idempotency_key", name="uq_event_attempts_participant_key"),
        Index("idx_event_attempts_event_rank", "event_id", "disqualified", "total_score"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id", ondelete="CASCADE"), nullable=False)
    participant_id: Mapped[int] = mapped_column(ForeignKey("event_participants.id", ondelete="CASCADE"), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(64), nullable=False)
    total_score: Mapped[float] = mapped_column(Float, nullable=False)
    disqualified: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    breakdown: Mapped[dict | None] = mapped_column(JSONB)
    hints: Mapped[list | None] = mapped_column(JSONB)
    usable_area_m2: Mapped[float] = mapped_column(Float, nullable=False)
    beat_ai: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    elapsed_seconds: Mapped[int | None] = mapped_column(Integer)
    layout_s3_url: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())

    participant: Mapped[EventParticipant] = relationship(back_populates="attempts")
