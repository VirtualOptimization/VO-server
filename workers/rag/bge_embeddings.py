"""Local BGE-M3 embedding helper (no per-call API cost, multilingual incl. Turkish/Korean)."""

from __future__ import annotations

from functools import lru_cache

EMBEDDING_MODEL_NAME = "BAAI/bge-m3"


@lru_cache(maxsize=1)
def _model():
    from sentence_transformers import SentenceTransformer

    return SentenceTransformer(EMBEDDING_MODEL_NAME)


def embed_texts(texts: list[str], batch_size: int = 32) -> list[list[float]]:
    """Embed a batch of texts with BGE-M3, returning plain Python float lists."""
    if not texts:
        return []
    vectors = _model().encode(
        texts,
        batch_size=batch_size,
        normalize_embeddings=True,
        show_progress_bar=False,
    )
    return [vector.tolist() for vector in vectors]


def embed_text(text: str) -> list[float]:
    return embed_texts([text])[0]
