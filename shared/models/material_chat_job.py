"""Material chatbot request and preview state."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from shared.db import Base


class MaterialChatJob(Base):
    """Tracks a user's natural-language material request until its preview is ready."""

    __tablename__ = "material_chat_jobs"
    __table_args__ = (
        Index("idx_material_chat_jobs_user_created", "user_id", "created_at"),
        Index("idx_material_chat_jobs_status", "status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    model_id: Mapped[int] = mapped_column(ForeignKey("furniture_models.id", ondelete="CASCADE"), nullable=False)
    texture_preset_id: Mapped[int | None] = mapped_column(
        ForeignKey("texture_presets.id", ondelete="SET NULL")
    )
    message: Mapped[str] = mapped_column(Text, nullable=False)
    assistant_message: Mapped[str | None] = mapped_column(Text)
    material_type: Mapped[str | None] = mapped_column(String(50))
    material_name: Mapped[str | None] = mapped_column(String(100))
    image_prompt: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default="PENDING")
    error_message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    user: Mapped["User"] = relationship()
    model: Mapped["FurnitureModel"] = relationship()
    texture_preset: Mapped["TexturePreset | None"] = relationship()
