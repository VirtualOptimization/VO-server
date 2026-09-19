"""Embed Neufert chunks with BGE-M3 and load them into Postgres (pgvector).

Replaces the local-JSONL + OpenAI-embedding workflow used by
select_extraction_pages.py: the corpus now lives in the same RDS instance the
rest of the service already talks to, so the running FastAPI app can query it
directly instead of shipping JSONL files around.

Usage:
    python -m workers.rag.ingest_neufert_chunks opt_RAG/neufert_chunks_tur.jsonl
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from shared.db import SessionLocal
from shared.models import NeufertChunk
from workers.rag.bge_embeddings import embed_texts

BATCH_SIZE = 32


def load_chunks(path: Path) -> list[dict[str, Any]]:
    chunks = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            chunks.append(json.loads(line))
    return chunks


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def main() -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("chunks_jsonl", type=Path)
    parser.add_argument("--batch-size", type=int, default=BATCH_SIZE)
    args = parser.parse_args()

    chunks = load_chunks(args.chunks_jsonl)
    print(f"청크 {len(chunks)}개 로드", flush=True)

    session = SessionLocal()
    try:
        existing_hashes = {row[0] for row in session.query(NeufertChunk.content_hash).all()}
        print(f"이미 적재된 청크: {len(existing_hashes)}개", flush=True)

        pending = [
            (chunk, content_hash(chunk.get("content", "")))
            for chunk in chunks
            if content_hash(chunk.get("content", "")) not in existing_hashes
        ]
        print(f"새로 임베딩할 청크: {len(pending)}개", flush=True)

        for start in range(0, len(pending), args.batch_size):
            batch = pending[start : start + args.batch_size]
            texts = [chunk.get("content", "") for chunk, _ in batch]
            vectors = embed_texts(texts, batch_size=args.batch_size)

            for (chunk, chash), vector in zip(batch, vectors, strict=True):
                session.add(
                    NeufertChunk(
                        source_name=chunk.get("source_name"),
                        page_start=chunk.get("page_start"),
                        page_end=chunk.get("page_end"),
                        section_title=chunk.get("section_title"),
                        content=chunk.get("content", ""),
                        content_hash=chash,
                        embedding=vector,
                    )
                )
            session.commit()
            print(f"{min(start + args.batch_size, len(pending))}/{len(pending)} 적재 완료", flush=True)

        total = session.query(NeufertChunk).count()
        print(f"완료. DB 내 총 청크 수: {total}")
    finally:
        session.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
