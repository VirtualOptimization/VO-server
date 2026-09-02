"""Pre-generated material variants for base furniture models."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from shared.db import Base


class FurnitureMaterialAsset(Base):
    """A reusable GLB/USDZ pair for one base-model and texture-preset combination."""

    __tablename__ = "furniture_material_assets"
    __table_args__ = (
        UniqueConstraint(
            "furniture_model_id",
            "texture_preset_id",
            name="uq_furniture_material_assets_model_preset",
        ),
        Index("idx_furniture_material_assets_status", "status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    furniture_model_id: Mapped[int] = mapped_column(
        ForeignKey("furniture_models.id", ondelete="CASCADE"), nullable=False
    )
    texture_preset_id: Mapped[int] = mapped_column(
        ForeignKey("texture_presets.id", ondelete="CASCADE"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default="PENDING")
    glb_url: Mapped[str | None] = mapped_column(Text)
    usdz_url: Mapped[str | None] = mapped_column(Text)
    error_message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    furniture_model: Mapped["FurnitureModel"] = relationship()
    texture_preset: Mapped["TexturePreset"] = relationship()
