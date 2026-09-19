"""Neufert reference chunk ORM model, embedded for pgvector similarity search."""

from __future__ import annotations

from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import DateTime, Index, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from shared.db import Base

# BAAI/bge-m3 dense embedding dimension.
BGE_M3_EMBEDDING_DIM = 1024


class NeufertChunk(Base):
    """A page-range chunk of the Neufert reference, embedded for RAG retrieval."""

    __tablename__ = "neufert_chunks"
    __table_args__ = (
        Index("idx_neufert_chunks_content_hash", "content_hash", unique=True),
        Index(
            "idx_neufert_chunks_embedding",
            "embedding",
            postgresql_using="hnsw",
            postgresql_with={"m": 16, "ef_construction": 64},
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    source_name: Mapped[str | None] = mapped_column(String(255))
    page_start: Mapped[int | None] = mapped_column(Integer)
    page_end: Mapped[int | None] = mapped_column(Integer)
    section_title: Mapped[str | None] = mapped_column(String(255))
    content: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    embedding: Mapped[list[float]] = mapped_column(Vector(BGE_M3_EMBEDDING_DIM), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
