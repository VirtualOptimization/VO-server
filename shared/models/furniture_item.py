"""Placed furniture item ORM model."""

from __future__ import annotations

from sqlalchemy import ForeignKey, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from shared.db import Base


class FurnitureItem(Base):
    """Concrete furniture placement within a version."""

    __tablename__ = "furniture_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    version_id: Mapped[int] = mapped_column(
        ForeignKey("versions.id", ondelete="CASCADE"), nullable=False
    )
    model_id: Mapped[int | None] = mapped_column(
        ForeignKey("furniture_models.id", ondelete="SET NULL")
    )
    item_key: Mapped[str] = mapped_column(String(100), nullable=False)

    pos_x: Mapped[float] = mapped_column(nullable=False)
    pos_y: Mapped[float] = mapped_column(nullable=False)
    pos_z: Mapped[float] = mapped_column(nullable=False)

    rot_x: Mapped[float] = mapped_column(nullable=False)
    rot_y: Mapped[float] = mapped_column(nullable=False)
    rot_z: Mapped[float] = mapped_column(nullable=False)

    scale_x: Mapped[float] = mapped_column(nullable=False, server_default="1.0")
    scale_y: Mapped[float] = mapped_column(nullable=False, server_default="1.0")
    scale_z: Mapped[float] = mapped_column(nullable=False, server_default="1.0")

    footprint_polygon: Mapped[dict | list | None] = mapped_column(JSONB)

    version: Mapped["Version"] = relationship(back_populates="furniture_items")
    model: Mapped["FurnitureModel | None"] = relationship(back_populates="furniture_items")
