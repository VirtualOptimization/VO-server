"""공통 S3 경로 헬퍼 — 여러 엔드포인트 파일에서 공유"""
from __future__ import annotations

import asyncio
import logging

from server.core.s3 import generate_presigned_url, get_json, head_object

logger = logging.getLogger(__name__)

RAW_ROOT_PREFIX = "scans"
CATALOG_PREFIX = "assets/roomplan-catalog/v1/usdc"
MODEL_CONTENT_TYPE = "application/octet-stream"
USDZ_CONTENT_TYPE = "model/vnd.usdz+zip"
JSON_CONTENT_TYPE = "application/json"
URL_EXPIRATION_SECONDS = 3600


def _raw_prefix(confirm_code: str) -> str:
    return f"{RAW_ROOT_PREFIX}/{confirm_code}/origin"


def _generated_prefix(confirm_code: str) -> str:
    return f"{RAW_ROOT_PREFIX}/{confirm_code}/optimized"


def _catalog_model_key(category: str, model_filename: str) -> str:
    """modelFileName + category → 카탈로그 S3 key"""
    base_name = model_filename.removesuffix(".rooms.usdc")
    return f"{CATALOG_PREFIX}/{category.capitalize()}/{base_name}/{base_name}.rooms.usdc"


async def _get_catalog_model_urls(raw_prefix: str) -> dict[str, str]:
    """room_data.json을 읽어 카탈로그 presigned URL 맵 반환"""
    try:
        data = await get_json(f"{raw_prefix}/room_data.json")
        model_urls: dict[str, str] = {}
        for obj in data.get("objects", []):
            model_filename = obj.get("modelFileName")
            category = obj.get("category", "")
            if model_filename and category:
                key = _catalog_model_key(category, model_filename)
                model_urls[model_filename] = generate_presigned_url(key)
        return model_urls
    except Exception as e:
        logger.warning(f"카탈로그 모델 URL 조회 실패: {e}")
        return {}


async def _resolve_prefixes(confirm_code: str) -> tuple[str, str] | None:
    """새 경로(scans/{code}/origin) 우선, 없으면 구 경로({code}/origin) 폴백."""
    new_raw = _raw_prefix(confirm_code)
    if await head_object(f"{new_raw}/room_data.json"):
        return new_raw, _generated_prefix(confirm_code)

    old_raw = f"{confirm_code}/origin"
    if await head_object(f"{old_raw}/room_data.json"):
        return old_raw, f"{confirm_code}/processed"

    return None
