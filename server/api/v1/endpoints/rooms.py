"""공간 조회 — 방 상태 / 버전 목록 / 버전 상세 / origin·optimized 에셋 다운로드"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import joinedload

from server.api.v1.deps import get_current_user
from server.api.v1.endpoints.rooms_common import _get_catalog_model_urls, _resolve_prefixes
from server.core.s3 import generate_presigned_url, generate_presigned_url_for_uri, head_object
from server.schemas.room_view import (
    FurnitureCatalogItemResponse,
    FurnitureCatalogResponse,
    MyRoomListItem,
    MyRoomListResponse,
    RoomSummaryResponse,
    RoomVersionItem,
    RoomVersionStateSummary,
    RoomVersionsResponse,
    VersionDetailResponse,
)
from server.schemas.scan import VersionAssetsResponse
from shared.db import SessionLocal
from shared.models.furniture_model import FurnitureModel
from shared.models.room import Room
from shared.models.user import User
from shared.models.version import Version

logger = logging.getLogger(__name__)
router = APIRouter()

MAX_VERSION_COUNT = 5
BASE_VERSION_COUNT = 2


def _unity_layout_uri(version: Version) -> str | None:
    json_data = version.json_data if isinstance(version.json_data, dict) else {}
    unity_uri = (
        json_data.get("unity_layout_json")
        or json_data.get("unity_roomplan_optimized_json")
    )
    if unity_uri:
        return unity_uri

    if version.s3_json_url and version.s3_json_url.endswith("/room_data.json"):
        return version.s3_json_url.removesuffix("/room_data.json") + "/room_data.unity.json"

    return None


def _to_version_detail_response(version: Version) -> VersionDetailResponse:
    return VersionDetailResponse(
        version_id=version.id,
        room_id=version.room_id,
        confirm_code=version.room.confirm_code if version.room else "",
        parent_version_id=version.parent_version_id,
        version_type=version.version_type,
        version_no=version.version_no,
        version_name=version.version_name,
        created_at=version.created_at,
        room_shell_url=version.room.room_shell_usdc_url if version.room else None,
        converted_glb_url=version.converted_glb_url,
        layout_json_url=generate_presigned_url_for_uri(version.s3_json_url),
        unity_layout_json_url=generate_presigned_url_for_uri(_unity_layout_uri(version)),
        json_data=version.json_data,
    )


def _version_state_summary(confirm_code: str, versions: list[Version]) -> RoomVersionStateSummary:
    version_types = {version.version_type for version in versions}
    user_edited_count = sum(1 for version in versions if version.version_type == "USER_EDITED")
    return RoomVersionStateSummary(
        confirm_code=confirm_code,
        has_original="ORIGINAL" in version_types,
        has_optimized="OPTIMIZED" in version_types,
        user_edited_count=user_edited_count,
    )


# ── GET /rooms  (로그인 사용자 공간 목록) ─────────────────────────────────────

@router.get("", response_model=MyRoomListResponse)
def get_my_rooms(current_user: User = Depends(get_current_user)):
    db = SessionLocal()
    try:
        rooms = (
            db.query(Room)
            .options(joinedload(Room.versions))
            .filter(Room.user_id == current_user.id)
            .order_by(Room.updated_at.desc(), Room.created_at.desc(), Room.id.desc())
            .all()
        )

        items: list[MyRoomListItem] = []
        for room in rooms:
            summary = _version_state_summary(room.confirm_code, room.versions)
            items.append(
                MyRoomListItem(
                    room_id=room.id,
                    confirm_code=room.confirm_code,
                    created_at=room.created_at,
                    has_original=summary.has_original,
                    has_optimized=summary.has_optimized,
                    user_edited_count=summary.user_edited_count,
                )
            )

        return MyRoomListResponse(rooms=items)
    finally:
        db.close()

# ── GET /rooms/{confirm_code}/versions/{version_id} ──────────────────────────

@router.get("/{confirm_code}/versions/{version_id}", response_model=VersionDetailResponse)
def get_room_version_detail(confirm_code: str, version_id: int):
    db = SessionLocal()
    try:
        version = (
            db.query(Version)
            .join(Room)
            .options(
                joinedload(Version.room),
            )
            .filter(
                Version.id == version_id,
                Room.confirm_code == confirm_code,
            )
            .first()
        )
        if version is None:
            raise HTTPException(status_code=404, detail="버전을 찾을 수 없습니다.")

        return _to_version_detail_response(version)
    finally:
        db.close()


# ── GET /rooms/catalog/models ────────────────────────────────────────────────

@router.get("/catalog/models", response_model=FurnitureCatalogResponse)
def get_furniture_catalog():
    db = SessionLocal()
    try:
        models = db.query(FurnitureModel).order_by(
            FurnitureModel.furniture_type,
            FurnitureModel.model_key,
        ).all()

        return FurnitureCatalogResponse(
            items=[
                FurnitureCatalogItemResponse(
                    model_key=model.model_key,
                    name=model.name,
                    furniture_type=model.furniture_type,
                    glb_url=generate_presigned_url_for_uri(model.glb_url),
                    width=float(model.width),
                    depth=float(model.depth),
                    height=float(model.height),
                )
                for model in models
            ]
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

@router.get("/{confirm_code}/versions", response_model=RoomVersionsResponse)
def get_room_versions(confirm_code: str):
    db = SessionLocal()
    try:
        room = (
            db.query(Room)
            .options(joinedload(Room.versions))
            .filter(Room.confirm_code == confirm_code)
            .first()
        )
        if room is None:
            raise HTTPException(status_code=404, detail="확인 코드를 찾을 수 없습니다.")

        versions = sorted(room.versions, key=lambda version: version.version_no)
        if not versions:
            raise HTTPException(status_code=404, detail="버전을 찾을 수 없습니다.")

        latest_version_no = max(version.version_no for version in versions)
        current_version_count = len(versions)
        return RoomVersionsResponse(
            room_id=room.id,
            confirm_code=room.confirm_code,
            current_version_count=current_version_count,
            max_version_count=MAX_VERSION_COUNT,
            can_create_user_version=current_version_count < MAX_VERSION_COUNT,
            remaining_user_edit_slots=max(
                0,
                MAX_VERSION_COUNT - max(BASE_VERSION_COUNT, current_version_count),
            ),
            versions=[
                RoomVersionItem(
                    version_id=version.id,
                    version_type=version.version_type,
                    version_no=version.version_no,
                    version_name=version.version_name,
                    created_at=version.created_at,
                    is_latest=version.version_no == latest_version_no,
                    can_delete=version.version_type == "USER_EDITED",
                )
                for version in versions
            ],
        )
    finally:
        db.close()


# ── GET /rooms/{confirm_code}/origin  (다운로드) ──────────────────────────────

@router.get("/{confirm_code}/origin", response_model=VersionAssetsResponse)
async def get_origin_assets(confirm_code: str):
    prefixes = await _resolve_prefixes(confirm_code)
    if prefixes is None:
        raise HTTPException(status_code=404, detail="확인 코드를 찾을 수 없습니다.")

    raw, _ = prefixes
    data_key = f"{raw}/room_data.json"
    unity_data_key = f"{raw}/room_data.unity.json"
    full_key  = f"{raw}/Room.usdz"
    empty_key = f"{raw}/Room_empty.usdz"
    glb_key = f"{raw}/output.glb"

    full_exists = await head_object(full_key) is not None
    empty_exists = await head_object(empty_key) is not None
    glb_exists = await head_object(glb_key) is not None
    unity_data_exists = await head_object(unity_data_key) is not None

    if not full_exists and not empty_exists and not glb_exists:
        raise HTTPException(status_code=404, detail="방 껍데기 GLB 또는 Room.usdz를 찾을 수 없습니다.")

    model_urls = await _get_catalog_model_urls(raw)

    return VersionAssetsResponse(
        usdz_url=generate_presigned_url(full_key) if full_exists else None,
        usdz_empty_url=generate_presigned_url(empty_key) if empty_exists else None,
        glb_url=generate_presigned_url(glb_key) if glb_exists else None,
        data_url=generate_presigned_url(data_key),
        unity_data_url=generate_presigned_url(unity_data_key) if unity_data_exists else None,
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
    unity_data_key = f"{gen}/room_data.roomplan_optimized.unity.json"

    if await head_object(data_key) is None:
        raise HTTPException(status_code=404, detail="최적화 데이터가 아직 없습니다.")
    unity_data_exists = await head_object(unity_data_key) is not None

    full_key  = f"{raw}/Room.usdz"
    empty_key = f"{raw}/Room_empty.usdz"
    glb_key = f"{raw}/output.glb"

    full_exists = await head_object(full_key) is not None
    empty_exists = await head_object(empty_key) is not None
    glb_exists = await head_object(glb_key) is not None

    if not full_exists and not empty_exists and not glb_exists:
        raise HTTPException(status_code=404, detail="방 껍데기 GLB 또는 Room.usdz를 찾을 수 없습니다.")

    model_urls = await _get_catalog_model_urls(raw)

    return VersionAssetsResponse(
        usdz_url=generate_presigned_url(full_key) if full_exists else None,
        usdz_empty_url=generate_presigned_url(empty_key) if empty_exists else None,
        glb_url=generate_presigned_url(glb_key) if glb_exists else None,
        data_url=generate_presigned_url(data_key),
        unity_data_url=generate_presigned_url(unity_data_key) if unity_data_exists else None,
        model_urls=model_urls,
    )
