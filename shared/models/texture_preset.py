"""Texture preset ORM model."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Index, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from shared.db import Base


class TexturePreset(Base):
    """Preset texture metadata used for material selection."""

    __tablename__ = "texture_presets"
    __table_args__ = (
        Index("idx_texture_presets_preset_key", "preset_key"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    preset_key: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    texture_s3_key: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
