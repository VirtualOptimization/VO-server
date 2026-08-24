"""Version ORM model."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKey, String, Text, UniqueConstraint, DateTime, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from shared.db import Base


class Version(Base):
    """A saved layout version for a room."""

    __tablename__ = "versions"
    __table_args__ = (
        CheckConstraint(
            "version_type IN ('ORIGINAL', 'OPTIMIZED', 'USER_EDITED')",
            name="chk_version_type",
        ),
        UniqueConstraint("room_id", "version_no", name="uq_room_id_version_no"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    room_id: Mapped[int] = mapped_column(
        ForeignKey("rooms.id", ondelete="CASCADE"), nullable=False
    )
    parent_version_id: Mapped[int | None] = mapped_column(
        ForeignKey("versions.id", ondelete="SET NULL")
    )
    version_type: Mapped[str] = mapped_column(String(20), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default="READY")
    version_no: Mapped[int] = mapped_column(nullable=False, server_default="0")
    s3_json_url: Mapped[str | None] = mapped_column(Text)
    converted_glb_url: Mapped[str | None] = mapped_column(Text)
    json_data: Mapped[dict | list | None] = mapped_column(JSONB)
    version_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    room: Mapped["Room"] = relationship(back_populates="versions")
    parent_version: Mapped["Version | None"] = relationship(
        remote_side="Version.id", back_populates="child_versions"
    )
    child_versions: Mapped[list["Version"]] = relationship(back_populates="parent_version")
    furniture_items: Mapped[list["FurnitureItem"]] = relationship(
        back_populates="version", cascade="all, delete-orphan", passive_deletes=True
    )
