"""Background conversion task ORM model."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from shared.db import Base


class ConversionTask(Base):
    """Queued asset conversion work processed outside the API service."""

    __tablename__ = "conversion_tasks"
    __table_args__ = (
        Index("idx_conversion_tasks_status", "status"),
        Index("idx_conversion_tasks_task_type", "task_type"),
        Index("idx_conversion_tasks_furniture_model_id", "furniture_model_id"),
        Index("idx_conversion_tasks_created_at", "created_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    task_type: Mapped[str] = mapped_column(String(50), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default="PENDING")
    source_key: Mapped[str | None] = mapped_column(Text)
    texture_key: Mapped[str | None] = mapped_column(Text)
    output_glb_key: Mapped[str | None] = mapped_column(Text)
    output_usdz_key: Mapped[str | None] = mapped_column(Text)
    furniture_model_id: Mapped[int | None] = mapped_column(
        ForeignKey("furniture_models.id", ondelete="SET NULL")
    )
    version_id: Mapped[int | None] = mapped_column(ForeignKey("versions.id", ondelete="SET NULL"))
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    error_message: Mapped[str | None] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    furniture_model: Mapped["FurnitureModel | None"] = relationship()
    version: Mapped["Version | None"] = relationship()
