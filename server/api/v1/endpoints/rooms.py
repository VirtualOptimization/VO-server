"""공간 조회 — 방 상태 / 버전 목록 / 버전 상세 / origin·optimized 에셋 다운로드"""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException
from sqlalchemy.orm import joinedload

from server.api.v1.endpoints.rooms_common import _list_model_keys, _resolve_prefixes, _raw_prefix
from server.core.s3 import generate_presigned_url, head_object
from server.schemas.room_view import (
    FurnitureItemView,
    RoomSummaryResponse,
    VersionDetailResponse,
)
from server.schemas.scan import ScanDetailResponse, VersionAssetsResponse, VersionSummary
from shared.db import SessionLocal
from shared.models.furniture_item import FurnitureItem
from shared.models.room import Room
from shared.models.version import Version

logger = logging.getLogger(__name__)
router = APIRouter()


# ── GET /rooms/versions/{version_id} ─────────────────────────────────────────
# ※ /{confirm_code} 보다 먼저 등록해야 경로 충돌 없음

def get_version_detail(version_id: int):
    db = SessionLocal()
    try:
        version = (
            db.query(Version)
            .options(
                joinedload(Version.room),
                joinedload(Version.furniture_items).joinedload(FurnitureItem.model),
            )
            .filter(Version.id == version_id)
            .first()
        )
        if version is None:
            raise HTTPException(status_code=404, detail="version not found")

        furniture_items = [
            FurnitureItemView(
                item_key=item.item_key,
                model_key=item.model.model_key if item.model else None,
                usdc_url=item.model.usdc_url if item.model else None,
                pos=[float(item.pos_x), float(item.pos_y), float(item.pos_z)],
                rot=[float(item.rot_x), float(item.rot_y), float(item.rot_z)],
                scale=[float(item.scale_x), float(item.scale_y), float(item.scale_z)],
            )
            for item in version.furniture_items
        ]

        return VersionDetailResponse(
            version_id=version.id, room_id=version.room_id,
            confirm_code=version.room.confirm_code if version.room else "",
            version_type=version.version_type, version_no=version.version_no,
            room_shell_url=version.room.room_shell_usdc_url if version.room else None,
            converted_glb_url=version.converted_glb_url,
            layout_json_url=version.s3_json_url, json_data=version.json_data,
            furniture_items=furniture_items,
        )
    finally:
        db.close()


# ── GET /rooms/{confirm_code} ─────────────────────────────────────────────────

@router.get("/{confirm_code}", response_model=RoomSummaryResponse)
def get_room_by_confirm_code(confirm_code: str):
    db = SessionLocal()
    try:
        room = db.query(Room).filter(Room.confirm_code == confirm_code).first()
        if room is None:
            raise HTTPException(status_code=404, detail="room not found")
        return RoomSummaryResponse(room_id=room.id, confirm_code=room.confirm_code, status=room.status)
    finally:
        db.close()


# ── GET /rooms/{confirm_code}/versions ────────────────────────────────────────

@router.get("/{confirm_code}/versions", response_model=ScanDetailResponse)
async def get_room_versions(confirm_code: str):
    prefixes = await _resolve_prefixes(confirm_code)
    if prefixes is None:
        raise HTTPException(status_code=404, detail="확인 코드를 찾을 수 없습니다.")

    raw, gen = prefixes
    candidates = [
        (f"{raw}/room_data.json",                    "origin"),
        (f"{gen}/room_data.roomplan_optimized.json", "optimized"),
    ]

    versions: list[VersionSummary] = []
    first_created_at = None
    for key, version_type in candidates:
        meta = await head_object(key)
        if meta is None:
            continue
        created_at = meta.get("LastModified")
        if first_created_at is None:
            first_created_at = created_at
        versions.append(VersionSummary(version_type=version_type, created_at=created_at))

    if not versions:
        raise HTTPException(status_code=404, detail="확인 코드를 찾을 수 없습니다.")

    return ScanDetailResponse(confirm_code=confirm_code, created_at=first_created_at, versions=versions)


# ── GET /rooms/{confirm_code}/origin  (다운로드) ──────────────────────────────

@router.get("/{confirm_code}/origin", response_model=VersionAssetsResponse)
async def get_origin_assets(confirm_code: str):
    prefixes = await _resolve_prefixes(confirm_code)
    if prefixes is None:
        raise HTTPException(status_code=404, detail="확인 코드를 찾을 수 없습니다.")

    raw, _ = prefixes
    data_key = f"{raw}/room_data.json"
    full_key  = f"{raw}/Room.usdz"
    empty_key = f"{raw}/Room_empty.usdz"

    if await head_object(full_key) is None:
        raise HTTPException(status_code=404, detail="Room.usdz를 찾을 수 없습니다.")
    usdz_key = empty_key if await head_object(empty_key) else full_key

    model_keys = await _list_model_keys(raw)
    model_urls = {key.split("/")[-1]: generate_presigned_url(key) for key in model_keys}

    glb_key = f"{raw}/output.glb"
    glb_url = generate_presigned_url(glb_key) if await head_object(glb_key) else None

    return VersionAssetsResponse(
        usdz_url=generate_presigned_url(usdz_key),
        glb_url=glb_url,
        data_url=generate_presigned_url(data_key),
        model_urls=model_urls,
    )


# ── GET /rooms/{confirm_code}/optimized  (다운로드) ───────────────────────────

@router.get("/{confirm_code}/optimized", response_model=VersionAssetsResponse)
async def get_optimized_assets(confirm_code: str):
    prefixes = await _resolve_prefixes(confirm_code)
    if prefixes is None:
        raise HTTPException(status_code=404, detail="확인 코드를 찾을 수 없습니다.")

    raw, gen = prefixes
    data_key = f"{gen}/room_data.roomplan_optimized.json"

    if await head_object(data_key) is None:
        raise HTTPException(status_code=404, detail="최적화 데이터가 아직 없습니다.")

    full_key  = f"{raw}/Room.usdz"
    empty_key = f"{raw}/Room_empty.usdz"

    if await head_object(full_key) is None:
        raise HTTPException(status_code=404, detail="Room.usdz를 찾을 수 없습니다.")
    usdz_key = empty_key if await head_object(empty_key) else full_key

    model_keys = await _list_model_keys(raw)
    model_urls = {key.split("/")[-1]: generate_presigned_url(key) for key in model_keys}

    # 🎯 하단 구문 에러 파편 완전히 청소 완료 및 origin 폴더(raw) 규칙 매핑 동기화
    glb_key = f"{raw}/output.glb"
    glb_url = generate_presigned_url(glb_key) if await head_object(glb_key) else None

    return VersionAssetsResponse(
        usdz_url=generate_presigned_url(usdz_key),
        glb_url=glb_url,
        data_url=generate_presigned_url(data_key),
        model_urls=model_urls,
    )