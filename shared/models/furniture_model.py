"""Furniture model catalog ORM model."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from shared.db import Base


class FurnitureModel(Base):
    """Master catalog entry for a recognized furniture model."""

    __tablename__ = "furniture_models"
    __table_args__ = (
        Index("idx_furniture_type", "furniture_type"),
        Index("idx_furniture_models_user_id", "user_id"),
        Index("idx_furniture_models_status", "status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    model_key: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    name: Mapped[str | None] = mapped_column(String(100))
    furniture_type: Mapped[str | None] = mapped_column(String(50))
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default="READY")
    glb_url: Mapped[str | None] = mapped_column(Text)
    width: Mapped[float] = mapped_column(nullable=False)
    depth: Mapped[float] = mapped_column(nullable=False)
    height: Mapped[float] = mapped_column(nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    furniture_items: Mapped[list["FurnitureItem"]] = relationship(back_populates="model")
    user: Mapped["User | None"] = relationship(back_populates="furniture_models")
