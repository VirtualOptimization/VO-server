"""공통 S3 경로 헬퍼 — 여러 엔드포인트 파일에서 공유"""
from __future__ import annotations

import asyncio
import logging
import re
from pathlib import PurePosixPath
from typing import Any

from server.core.s3 import generate_presigned_url, get_json, head_object

logger = logging.getLogger(__name__)

RAW_ROOT_PREFIX = "scans"
CATALOG_PREFIX = "asset"
MODEL_CONTENT_TYPE = "application/octet-stream"
USDZ_CONTENT_TYPE = "model/vnd.usdz+zip"
JSON_CONTENT_TYPE = "application/json"
URL_EXPIRATION_SECONDS = 3600

CATALOG_CATEGORY_DIRS = {
    "bed": "Bed",
    "chair": "Chair",
    "sofa": "Sofa",
    "storage": "Storage",
    "table": "Table",
}

CATALOG_VARIANT_BY_FILENAME = {
    "DefaultChair": "Default",
    "DefaultTable": "Default",
    "shelf_vertical": "Shelf",
}

def _safe_s3_segment(value: str | None, fallback: str) -> str:
    source = value or fallback
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", source).strip("._-")
    return safe or fallback


def _user_s3_segment(user: Any | None) -> str | None:
    if user is None:
        return None
    user_id = getattr(user, "id", None)
    return _safe_s3_segment(getattr(user, "login_id", None), f"user_{user_id or 'unknown'}")


def _scan_root(room_ref: str, owner_segment: str | None = None) -> str:
    if owner_segment:
        return f"{owner_segment}/{RAW_ROOT_PREFIX}/{room_ref}"
    return f"{RAW_ROOT_PREFIX}/{room_ref}"


def _raw_prefix(room_ref: str, owner_segment: str | None = None) -> str:
    return f"{_scan_root(room_ref, owner_segment)}/origin"


def _generated_prefix(room_ref: str, owner_segment: str | None = None) -> str:
    return f"{_scan_root(room_ref, owner_segment)}/optimized"


def _catalog_model_key(category: str, model_filename: str) -> str:
    """modelFileName + category → 카탈로그 GLB S3 key"""
    category_dir = CATALOG_CATEGORY_DIRS.get(category.lower(), category.capitalize())
    path = PurePosixPath(model_filename.replace("\\", "/"))
    filename = path.name
    base_name = filename.removesuffix(".rooms.usdc")
    variant = CATALOG_VARIANT_BY_FILENAME.get(base_name, path.parent.name if path.parent.name else base_name)
    glb_name = base_name.replace("_lShaped", "_lshaped")
    return f"{CATALOG_PREFIX}/{category_dir}/{variant}/{glb_name}.rooms.glb"


def _catalog_db_model_key(category: str, model_filename: str) -> str:
    """modelFileName + category → furniture_models.model_key"""
    return (
        _catalog_model_key(category, model_filename)
        .removeprefix(f"{CATALOG_PREFIX}/")
        .removesuffix(".rooms.glb")
        + ".rooms.usdc"
    )


async def _get_catalog_model_urls(raw_prefix: str) -> dict[str, str]:
    """room_data.json을 읽어 카탈로그 GLB presigned URL 맵을 반환한다."""
    model_urls, _ = await _get_catalog_model_asset_urls(raw_prefix)
    return model_urls


async def _get_catalog_model_asset_urls(raw_prefix: str) -> tuple[dict[str, str], dict[str, str]]:
    """방에 배치된 카탈로그 가구의 GLB·USDZ presigned URL 맵을 반환한다."""
    try:
        data = await get_json(f"{raw_prefix}/room_data.json")
        catalog_assets: dict[str, tuple[str, str]] = {}
        for obj in data.get("objects", []):
            model_filename = obj.get("modelFileName")
            category = obj.get("category", "")
            if model_filename and category:
                glb_key = _catalog_model_key(category, model_filename)
                model_key = _catalog_db_model_key(category, model_filename)
                catalog_assets[model_key] = (glb_key, glb_key.removesuffix(".glb") + ".usdz")

        model_urls = {
            model_key: generate_presigned_url(glb_key)
            for model_key, (glb_key, _) in catalog_assets.items()
        }

        model_usdz_urls: dict[str, str] = {}
        if catalog_assets:
            asset_items = list(catalog_assets.items())
            usdz_results = await asyncio.gather(
                *(head_object(usdz_key) for _, (_, usdz_key) in asset_items)
            )
            model_usdz_urls = {
                model_key: generate_presigned_url(usdz_key)
                for (model_key, (_, usdz_key)), metadata in zip(asset_items, usdz_results)
                if metadata is not None
            }

        return model_urls, model_usdz_urls
    except Exception as e:
        logger.warning(f"카탈로그 모델 URL 조회 실패: {e}")
        return {}, {}


def _find_model_asset_url(asset_urls: dict[str, str], model_key: str | None) -> str | None:
    """정규화 전 파일명 키까지 고려해 모델 에셋 URL을 찾는다."""
    if not model_key:
        return None
    if model_key in asset_urls:
        return asset_urls[model_key]

    filename = PurePosixPath(model_key.replace("\\", "/")).name
    matches = [
        url
        for candidate_key, url in asset_urls.items()
        if PurePosixPath(candidate_key).name == filename
    ]
    return matches[0] if len(matches) == 1 else None


def _attach_model_asset_urls(
    objects: list[dict[str, Any]],
    model_urls: dict[str, str],
    model_usdz_urls: dict[str, str],
) -> list[dict[str, Any]]:
    """각 가구 객체에 클라이언트가 바로 사용할 GLB·USDZ URL을 추가한다."""
    return [
        {
            **obj,
            "glb_url": _find_model_asset_url(model_urls, obj.get("model_key")),
            "usdz_url": _find_model_asset_url(model_usdz_urls, obj.get("model_key")),
        }
        for obj in objects
    ]


async def _resolve_prefixes(room_ref: str, owner_segment: str | None = None) -> tuple[str, str] | None:
    """사용자 경로 우선, 없으면 기존 scans/{code} 및 레거시 경로 폴백."""
    if owner_segment:
        owned_raw = _raw_prefix(room_ref, owner_segment)
        if await head_object(f"{owned_raw}/room_data.json"):
            return owned_raw, _generated_prefix(room_ref, owner_segment)

    new_raw = _raw_prefix(room_ref)
    if await head_object(f"{new_raw}/room_data.json"):
        return new_raw, _generated_prefix(room_ref)

    old_raw = f"{room_ref}/origin"
    if await head_object(f"{old_raw}/room_data.json"):
        return old_raw, f"{room_ref}/processed"

    return None
