"""Select candidate Neufert pages for rule extraction via hybrid BM25 + embedding search.

The extraction pipeline used to scan a hand-picked page range (232-261). That
range is whatever a human happened to notice while skimming the table of
contents, so it silently misses relevant sections elsewhere in the book. This
script instead pools BM25 (lexical) and OpenAI-embedding (semantic) top-K hits
across a set of single-occupant small-room queries -- the same "reduce
single-method bias by unioning result sets" idea behind TREC pooling -- and
reports which pages are relevant but currently outside the extraction range.

Usage:
    python workers/rag/select_extraction_pages.py \
        opt_RAG/neufert_chunks_tur.jsonl \
        --embedding-cache opt_RAG/neufert_chunk_embeddings.jsonl \
        --output opt_RAG/page_candidates_hybrid.json \
        --current-pages 232-261
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
from collections import Counter
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from openai import OpenAI

TOKEN_RE = re.compile(r"[a-zçğıöşüâîû0-9]+", re.IGNORECASE)
EMBEDDING_MODEL = "text-embedding-3-small"

# Turkish queries covering the single-occupant small-room topics we care about,
# each with a short English label for the report.
QUERIES: list[tuple[str, str]] = [
    ("yatak odası mobilya yerleşimi ve mesafeleri", "bedroom furniture placement/clearance"),
    ("çalışma masası ve sandalye arasındaki mesafe", "desk-chair clearance"),
    ("gömme dolap ve gardırop yerleşimi", "built-in wardrobe/closet placement"),
    ("tek kişilik konut küçük oda düzeni", "single-occupant small room layout"),
    ("oda içinde hareket ve geçiş genişliği", "in-room movement/passage width"),
    ("kapı önü ve pencere önü mesafe", "door/window clearance"),
    ("katlanır yatak ve çok amaçlı mobilya", "folding bed / multifunctional furniture"),
    ("raf ve depolama alanı yerleşimi", "shelf/storage placement"),
    ("banyo küvet ve lavabo mesafeleri", "bathroom bathtub/sink clearance"),
    ("mutfak çalışma yüzeyi ve dolap mesafeleri", "kitchenette work surface/cabinet clearance"),
]

TOP_K_PER_METHOD = 15


def tokenize(text: str) -> list[str]:
    return TOKEN_RE.findall(text.lower())


class BM25:
    """Minimal BM25 (Okapi) implementation -- no extra dependency needed."""

    def __init__(self, docs_tokens: list[list[str]], k1: float = 1.5, b: float = 0.75):
        self.k1 = k1
        self.b = b
        self.n = len(docs_tokens)
        self.doc_lens = [len(d) for d in docs_tokens]
        self.avgdl = (sum(self.doc_lens) / self.n) if self.n else 0.0
        self.doc_term_freqs = [Counter(d) for d in docs_tokens]
        df: Counter[str] = Counter()
        for doc in docs_tokens:
            df.update(set(doc))
        self.idf = {
            term: math.log(1 + (self.n - freq + 0.5) / (freq + 0.5))
            for term, freq in df.items()
        }

    def top_k(self, query_tokens: list[str], k: int) -> list[tuple[int, float]]:
        scores = []
        for idx in range(self.n):
            tf = self.doc_term_freqs[idx]
            dl = self.doc_lens[idx]
            score = 0.0
            for term in query_tokens:
                f = tf.get(term)
                if not f:
                    continue
                idf = self.idf.get(term, 0.0)
                score += idf * (f * (self.k1 + 1)) / (f + self.k1 * (1 - self.b + self.b * dl / self.avgdl))
            if score > 0:
                scores.append((idx, score))
        scores.sort(key=lambda pair: pair[1], reverse=True)
        return scores[:k]


def load_chunks(path: Path) -> list[dict[str, Any]]:
    chunks = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            chunks.append(json.loads(line))
    return chunks


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def load_embedding_cache(path: Path | None) -> dict[str, list[float]]:
    if not path or not path.exists():
        return {}
    cache = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            row = json.loads(line)
            cache[row["hash"]] = row["embedding"]
    return cache


def save_embedding_cache(path: Path, cache: dict[str, list[float]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps({"hash": h, "embedding": v}) for h, v in cache.items()) + "\n",
        encoding="utf-8",
    )


def embed_texts(client: OpenAI, texts: list[str], batch_size: int = 100) -> list[list[float]]:
    vectors: list[list[float]] = []
    for i in range(0, len(texts), batch_size):
        batch = texts[i : i + batch_size]
        response = client.embeddings.create(model=EMBEDDING_MODEL, input=batch)
        vectors.extend(item.embedding for item in response.data)
    return vectors


def cosine_top_k(query_vec: list[float], doc_vecs, k: int) -> list[tuple[int, float]]:
    import numpy as np

    q = np.array(query_vec, dtype=float)
    q = q / (np.linalg.norm(q) + 1e-12)
    d = np.array(doc_vecs, dtype=float)
    norms = np.linalg.norm(d, axis=1) + 1e-12
    d = d / norms[:, None]
    scores = d @ q
    order = np.argsort(-scores)[:k]
    return [(int(i), float(scores[i])) for i in order]


def parse_page_range(value: str) -> range:
    start, end = value.split("-")
    return range(int(start), int(end) + 1)


def main() -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("chunks_jsonl", type=Path)
    parser.add_argument("--embedding-cache", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--current-pages", default="232-261", help="Existing extraction range, e.g. 232-261")
    parser.add_argument("--top-k", type=int, default=TOP_K_PER_METHOD)
    args = parser.parse_args()

    if not os.getenv("OPENAI_API_KEY"):
        raise RuntimeError("OPENAI_API_KEY가 .env 또는 환경변수에 없습니다.")

    chunks = load_chunks(args.chunks_jsonl)
    print(f"청크 {len(chunks)}개 로드", flush=True)

    docs_tokens = [tokenize(chunk.get("content", "")) for chunk in chunks]
    bm25 = BM25(docs_tokens)
    print("BM25 인덱스 구축 완료", flush=True)

    client = OpenAI(timeout=60.0, max_retries=2)
    cache = load_embedding_cache(args.embedding_cache)
    hashes = [content_hash(chunk.get("content", "")) for chunk in chunks]
    missing_idx = [i for i, h in enumerate(hashes) if h not in cache]
    print(f"임베딩 캐시 hit={len(chunks) - len(missing_idx)} miss={len(missing_idx)}", flush=True)
    if missing_idx:
        missing_texts = [chunks[i].get("content", "") for i in missing_idx]
        vectors = embed_texts(client, missing_texts)
        for i, vec in zip(missing_idx, vectors):
            cache[hashes[i]] = vec
        save_embedding_cache(args.embedding_cache, cache)
        print(f"신규 임베딩 {len(missing_idx)}개 계산 및 캐시 저장", flush=True)

    doc_vecs = [cache[h] for h in hashes]

    current_pages = set(parse_page_range(args.current_pages))
    page_hits: dict[int, dict[str, Any]] = {}

    for query_text, label in QUERIES:
        query_tokens = tokenize(query_text)
        bm25_hits = bm25.top_k(query_tokens, args.top_k)
        query_vec = embed_texts(client, [query_text])[0]
        embed_hits = cosine_top_k(query_vec, doc_vecs, args.top_k)

        pooled: dict[int, dict[str, float]] = {}
        for idx, score in bm25_hits:
            pooled.setdefault(idx, {})["bm25"] = score
        for idx, score in embed_hits:
            pooled.setdefault(idx, {})["embedding"] = score

        for idx, method_scores in pooled.items():
            chunk = chunks[idx]
            for page in range(int(chunk.get("page_start", 0)), int(chunk.get("page_end", chunk.get("page_start", 0))) + 1):
                entry = page_hits.setdefault(
                    page,
                    {"page": page, "section_titles": set(), "matches": []},
                )
                entry["section_titles"].add(chunk.get("section_title") or "")
                entry["matches"].append(
                    {
                        "query": query_text,
                        "query_label": label,
                        "methods": sorted(method_scores.keys()),
                        "scores": {k: round(v, 4) for k, v in method_scores.items()},
                    }
                )
        print(f"쿼리 처리 완료: {label} (bm25={len(bm25_hits)} embedding={len(embed_hits)} pooled={len(pooled)})", flush=True)

    results = []
    for page, entry in sorted(page_hits.items()):
        results.append(
            {
                "page": page,
                "already_in_current_range": page in current_pages,
                "section_titles": sorted(t for t in entry["section_titles"] if t),
                "num_matching_queries": len({m["query"] for m in entry["matches"]}),
                "matches": entry["matches"],
            }
        )

    new_pages = [r for r in results if not r["already_in_current_range"]]
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"\n총 후보 페이지: {len(results)}개 (기존 범위 밖 신규: {len(new_pages)}개)")
    print(f"결과 저장: {args.output}")
    print("\n=== 기존 범위 밖 신규 후보 페이지 (질의 매칭 수 순) ===")
    for r in sorted(new_pages, key=lambda x: -x["num_matching_queries"])[:30]:
        titles = ", ".join(r["section_titles"][:2])
        print(f"  page {r['page']:>4}  matched_queries={r['num_matching_queries']}  {titles}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
