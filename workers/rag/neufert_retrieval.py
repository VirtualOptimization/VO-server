"""Hybrid (pgvector + Postgres full-text) search over Neufert chunks, DB-backed.

Replaces the standalone select_extraction_pages.py workflow (local JSONL +
in-memory BM25 + OpenAI embeddings, rebuilt from scratch on every run) now
that the corpus lives in Postgres: the index already exists in the DB, so any
process with a DB connection -- including the running FastAPI service -- can
query it directly instead of shipping and re-indexing JSONL files.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import text

from shared.db import SessionLocal
from workers.rag.bge_embeddings import embed_text


@dataclass
class ChunkMatch:
    id: int
    page_start: int | None
    page_end: int | None
    section_title: str | None
    content: str
    vector_score: float | None
    text_score: float | None


def _vector_literal(vector: list[float]) -> str:
    return "[" + ",".join(f"{v:.8f}" for v in vector) + "]"


def search_chunks(query: str, top_k: int = 15) -> list[ChunkMatch]:
    """Pool pgvector cosine-similarity hits with Postgres full-text hits for one query.

    Mirrors the TREC-pooling idea behind the original hybrid script: union the
    result sets of two different retrieval methods instead of trusting either
    one alone, so a chunk only the lexical or only the semantic side finds
    still surfaces.
    """
    query_vector = _vector_literal(embed_text(query))
    session = SessionLocal()
    try:
        vector_rows = session.execute(
            text(
                """
                SELECT id, page_start, page_end, section_title, content,
                       1 - (embedding <=> CAST(:query_vector AS vector)) AS vector_score
                FROM neufert_chunks
                ORDER BY embedding <=> CAST(:query_vector AS vector)
                LIMIT :top_k
                """
            ),
            {"query_vector": query_vector, "top_k": top_k},
        ).mappings().all()

        text_rows = session.execute(
            text(
                """
                SELECT id, page_start, page_end, section_title, content,
                       ts_rank_cd(to_tsvector('simple', content), plainto_tsquery('simple', :query_text)) AS text_score
                FROM neufert_chunks
                WHERE to_tsvector('simple', content) @@ plainto_tsquery('simple', :query_text)
                ORDER BY text_score DESC
                LIMIT :top_k
                """
            ),
            {"query_text": query, "top_k": top_k},
        ).mappings().all()
    finally:
        session.close()

    pooled: dict[int, dict[str, Any]] = {}
    for row in vector_rows:
        pooled.setdefault(row["id"], dict(row))
    for row in text_rows:
        entry = pooled.setdefault(row["id"], dict(row))
        entry["text_score"] = row["text_score"]

    return [
        ChunkMatch(
            id=row["id"],
            page_start=row.get("page_start"),
            page_end=row.get("page_end"),
            section_title=row.get("section_title"),
            content=row["content"],
            vector_score=row.get("vector_score"),
            text_score=row.get("text_score"),
        )
        for row in pooled.values()
    ]
