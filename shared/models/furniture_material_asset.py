"""Furniture material asset ORM model."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from shared.db import Base


class FurnitureMaterialAsset(Base):
    """A user-owned GLB/USDZ variant of a base furniture asset."""

    __tablename__ = "furniture_material_assets"
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "room_id",
            "version_id",
            "furniture_instance_id",
            "base_model_id",
            name="uq_furniture_material_asset",
        ),
        Index("idx_furniture_material_assets_user_id", "user_id"),
        Index("idx_furniture_material_assets_base_model_id", "base_model_id"),
        Index("idx_furniture_material_assets_status", "status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    base_model_id: Mapped[int] = mapped_column(
        ForeignKey("furniture_models.id", ondelete="CASCADE"), nullable=False
    )
    room_id: Mapped[int] = mapped_column(ForeignKey("rooms.id", ondelete="CASCADE"), nullable=False)
    version_id: Mapped[int] = mapped_column(ForeignKey("versions.id", ondelete="CASCADE"), nullable=False)
    furniture_instance_id: Mapped[str] = mapped_column(String(100), nullable=False)
    material_preset_id: Mapped[str] = mapped_column(String(100), nullable=False)
    material_name: Mapped[str | None] = mapped_column(String(100))
    glb_url: Mapped[str | None] = mapped_column(Text)
    usdz_url: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default="UPLOADING")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    user: Mapped["User"] = relationship(back_populates="furniture_material_assets")
    base_model: Mapped["FurnitureModel"] = relationship(back_populates="material_assets")
    room: Mapped["Room"] = relationship()
    version: Mapped["Version"] = relationship()
