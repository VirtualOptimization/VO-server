"""Furniture model catalog ORM model."""

from __future__ import annotations

from sqlalchemy import Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from shared.db import Base


class FurnitureModel(Base):
    """Master catalog entry for a recognized furniture model."""

    __tablename__ = "furniture_models"
    __table_args__ = (Index("idx_furniture_type", "furniture_type"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    model_key: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    name: Mapped[str | None] = mapped_column(String(100))
    furniture_type: Mapped[str | None] = mapped_column(String(50))
    usdc_url: Mapped[str | None] = mapped_column(Text)
    width: Mapped[float] = mapped_column(nullable=False)
    depth: Mapped[float] = mapped_column(nullable=False)
    height: Mapped[float] = mapped_column(nullable=False)

    furniture_items: Mapped[list["FurnitureItem"]] = relationship(back_populates="model")

