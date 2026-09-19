"""Room ORM model."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from shared.db import Base


class Room(Base):
    """Master record for a scanned room."""

    __tablename__ = "rooms"
    __table_args__ = (
        Index("idx_rooms_confirm_code", "confirm_code"),
        Index("idx_rooms_user_id", "user_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    confirm_code: Mapped[str] = mapped_column(String(6), unique=True, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default="PENDING")
    room_shell_usdc_url: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    versions: Mapped[list["Version"]] = relationship(
        back_populates="room", cascade="all, delete-orphan", passive_deletes=True
    )
    user: Mapped["User | None"] = relationship(back_populates="rooms")
