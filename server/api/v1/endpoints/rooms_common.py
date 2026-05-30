"""공통 S3 경로 헬퍼 — 여러 엔드포인트 파일에서 공유"""
from __future__ import annotations

import asyncio

from server.core.s3 import head_object, list_keys

RAW_ROOT_PREFIX = "scans"
MODEL_CONTENT_TYPE = "application/octet-stream"
USDZ_CONTENT_TYPE = "model/vnd.usdz+zip"
JSON_CONTENT_TYPE = "application/json"
URL_EXPIRATION_SECONDS = 3600


def _raw_prefix(confirm_code: str) -> str:
    return f"{RAW_ROOT_PREFIX}/{confirm_code}/origin"


def _generated_prefix(confirm_code: str) -> str:
    return f"{RAW_ROOT_PREFIX}/{confirm_code}/optimized"


async def _list_model_keys(raw_prefix: str) -> list[str]:
    """모델 파일 목록 조회"""
    keys = await asyncio.to_thread(list_keys, f"{raw_prefix}/models/")
    return [k for k in keys if not k.endswith("/")]


async def _resolve_prefixes(confirm_code: str) -> tuple[str, str] | None:
    """새 경로(scans/{code}/origin) 우선, 없으면 구 경로({code}/origin) 폴백."""
    new_raw = _raw_prefix(confirm_code)
    if await head_object(f"{new_raw}/room_data.json"):
        return new_raw, _generated_prefix(confirm_code)

    old_raw = f"{confirm_code}/origin"
    if await head_object(f"{old_raw}/room_data.json"):
        return old_raw, f"{confirm_code}/processed"

    return None
