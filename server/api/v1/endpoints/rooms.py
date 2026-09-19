"""공간 조회 — 방 상태 / 버전 목록 / 버전 상세 / origin·optimized 에셋 다운로드"""
from __future__ import annotations

import logging

import json 

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import or_
from sqlalchemy.orm import Session, joinedload

from server.api.v1.deps import get_current_user
from server.api.v1.endpoints.rooms_common import (
    _attach_model_asset_urls,
    _catalog_db_model_key,
    _get_catalog_model_asset_urls,
    _resolve_prefixes,
    _user_s3_segment,
)
from server.core.s3 import (
    generate_presigned_url,
    generate_presigned_url_for_uri,
    get_s3_client,
    head_object,
    parse_s3_uri,
)
from server.core.config import settings
from server.schemas.room_view import (
    FurnitureCatalogItemResponse,
    FurnitureCatalogResponse,
    MyRoomListItem,
    MyRoomListResponse,
    RoomNameUpdateRequest,
    RoomNameUpdateResponse,
    RoomSummaryResponse,
    RoomVersionItem,
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
OLD_CATALOG_GLB_PATH = "/assets/roomplan-catalog/v1/glb/"
NEW_CATALOG_GLB_PATH = "/asset/"


def _catalog_glb_uri(model: FurnitureModel) -> str | None:
    """공용 카탈로그 모델은 새 S3 asset 경로로 응답한다."""
    if not model.glb_url:
        return None
    if model.user_id is None:
        return model.glb_url.replace(OLD_CATALOG_GLB_PATH, NEW_CATALOG_GLB_PATH)
    return model.glb_url


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


def _to_version_detail_response(
    version: Version,
    *,
    usdz_empty_url: str | None = None,
    model_urls: dict[str, str] | None = None,
    model_usdz_urls: dict[str, str] | None = None,
    objects: list[dict] | None = None,
    ios_objects: list[dict] | None = None,
) -> VersionDetailResponse:
    return VersionDetailResponse(
        version_id=version.id,
        room_id=version.room_id,
        parent_version_id=version.parent_version_id,
        version_type=version.version_type,
        version_no=version.version_no,
        version_name=version.version_name,
        status=version.status,
        created_at=version.created_at,
        room_shell_url=version.room.room_shell_usdc_url if version.room else None,
        converted_glb_url=version.converted_glb_url,
        layout_json_url=generate_presigned_url_for_uri(version.s3_json_url),
        unity_layout_json_url=generate_presigned_url_for_uri(_unity_layout_uri(version)),
        usdz_empty_url=usdz_empty_url,
        model_urls=model_urls or {},
        model_usdz_urls=model_usdz_urls or {},
        objects=objects or [],
        ios_objects=ios_objects or [],
        json_data=version.json_data,
    )


def _key_from_s3_uri(uri: str | None) -> str | None:
    parsed = parse_s3_uri(uri)
    if parsed is None:
        return None
    bucket, key = parsed
    if bucket != settings.s3_bucket_name:
        return None
    return key


def _version_state_summary(versions: list[Version]) -> dict[str, int | bool]:
    version_types = {version.version_type for version in versions}
    user_edited_count = sum(1 for version in versions if version.version_type == "USER_EDITED")
    return {
        "has_original": "ORIGINAL" in version_types,
        "has_optimized": "OPTIMIZED" in version_types,
        "user_edited_count": user_edited_count,
    }


def _catalog_model_db_aliases(model: FurnitureModel) -> set[str]:
    """Return DB/model JSON key variants that can refer to the same catalog model."""
    model_key = model.model_key
    filename = model_key.rsplit("/", 1)[-1]
    stem = filename.removesuffix(".rooms.usdc").removesuffix(".usdc").removesuffix(".glb")
    category_from_path = model_key.split("/", 1)[0].lower() if "/" in model_key else None
    variant_from_path = model_key.split("/")[-2] if "/" in model_key and len(model_key.split("/")) >= 2 else None

    category_aliases = {
        alias
        for alias in {
            (model.furniture_type or "").strip().lower(),
            category_from_path,
        }
        if alias
    }
    if "storage" in category_aliases:
        category_aliases.add("shelf")
    if "shelf" in category_aliases:
        category_aliases.add("storage")

    aliases = {model_key, filename, stem}
    for category in category_aliases:
        if stem:
            aliases.add(f"{category}:{stem}")
        if variant_from_path:
            aliases.add(f"{category}:{variant_from_path}")

    return {alias for alias in aliases if alias}


def _get_room_objects_with_model_ids(
    db: Session,
    unity_data_key: str,
) -> list[dict]:
    """unity_data_key JSON을 읽어와 가구 인스턴스별 model_id를 매핑한 objects 목록을 반환한다."""
    try:
        s3_client = get_s3_client()
        response = s3_client.get_object(Bucket=settings.s3_bucket_name, Key=unity_data_key)
        content = response["Body"].read().decode("utf-8")
        unity_json = json.loads(content)
    except Exception as e:
        logger.warning(f"Failed to read or parse unity json at {unity_data_key}: {e}")
        return []

    raw_objects = unity_json.get("objects") or []
    if not isinstance(raw_objects, list):
        return []

    def object_model_key(obj: dict) -> str | None:
        model_key = obj.get("modelKey") or obj.get("model_key")
        if model_key:
            return model_key

        model_filename = obj.get("modelFileName")
        category = obj.get("category")
        if model_filename and category:
            return _catalog_db_model_key(category, model_filename)
        return model_filename

    model_keys = {
        model_key
        for obj in raw_objects
        if isinstance(obj, dict) and (model_key := object_model_key(obj))
    }

    models = (
        db.query(FurnitureModel)
        .filter(
            FurnitureModel.status != "DELETED",
            or_(
                FurnitureModel.user_id.is_(None),
                FurnitureModel.model_key.in_(model_keys),
            ),
        )
        .all()
    )
    model_map: dict[str, tuple[int, str]] = {}
    for model in models:
        for alias in _catalog_model_db_aliases(model):
            model_map.setdefault(alias, (model.id, model.model_key))

    objects_result = []
    for obj in raw_objects:
        if not isinstance(obj, dict):
            continue
        model_key = object_model_key(obj)
        identifier = obj.get("identifier")
        matched_model = model_map.get(model_key)
        resolved_model_key = matched_model[1] if matched_model else model_key
        resolved_model_id = matched_model[0] if matched_model else None

        objects_result.append(
            {
                **obj,
                "identifier": identifier,
                "model_key": resolved_model_key,
                "model_id": resolved_model_id,
            }
        )

    return objects_result

# ── GET /rooms  (로그인 사용자 공간 목록) ─────────────────────────────────────

@router.get("", response_model=MyRoomListResponse)
def get_my_rooms(current_user: User = Depends(get_current_user)):
    db = SessionLocal()
    try:
        rooms = (
            db.query(Room)
            .options(joinedload(Room.versions))
            .filter(
                Room.user_id == current_user.id,
                Room.status == "COMPLETED",
            )
            .order_by(Room.updated_at.desc(), Room.created_at.desc(), Room.id.desc())
            .all()
        )

        items: list[MyRoomListItem] = []
        for room in rooms:
            summary = _version_state_summary(room.versions)
            items.append(
                MyRoomListItem(
                    room_id=room.id,
                    name=room.name,
                    created_at=room.created_at,
                    has_original=bool(summary["has_original"]),
                    has_optimized=bool(summary["has_optimized"]),
                    user_edited_count=int(summary["user_edited_count"]),
                )
            )

        return MyRoomListResponse(rooms=items)
    finally:
        db.close()


# ── PATCH /rooms/{room_id}  (방 이름 수정) ───────────────────────────────────

@router.patch("/{room_id}", response_model=RoomNameUpdateResponse)
def update_room_name(
    room_id: int,
    payload: RoomNameUpdateRequest,
    current_user: User = Depends(get_current_user),
):
    db = SessionLocal()
    try:
        room = db.query(Room).filter(Room.id == room_id, Room.user_id == current_user.id).first()
        if room is None:
            raise HTTPException(status_code=404, detail="방을 찾을 수 없습니다.")

        room.name = payload.name.strip() if payload.name and payload.name.strip() else None
        db.commit()
        db.refresh(room)
        return RoomNameUpdateResponse(room_id=room.id, name=room.name)
    finally:
        db.close()


# ── GET /rooms/{room_id}/versions/{version_id} ───────────────────────────────

@router.get("/{room_id}/versions/{version_id}", response_model=VersionDetailResponse)
async def get_room_version_detail(
    room_id: int,
    version_id: int,
    current_user: User = Depends(get_current_user),
):
    db = SessionLocal()
    try:
        version = (
            db.query(Version)
            .options(
                joinedload(Version.room).joinedload(Room.user),
            )
            .join(Room)
            .filter(
                Version.id == version_id,
                Version.room_id == room_id,
                Room.user_id == current_user.id,
            )
            .first()
        )
        if version is None:
            raise HTTPException(status_code=404, detail="버전을 찾을 수 없습니다.")

        owner_segment = _user_s3_segment(version.room.user)
        prefixes = await _resolve_prefixes(str(room_id), owner_segment)
        if prefixes is None:
            return _to_version_detail_response(version)

        raw, _ = prefixes
        empty_key = f"{raw}/Room_empty.usdz"
        empty_exists = await head_object(empty_key) is not None
        model_urls, model_usdz_urls = await _get_catalog_model_asset_urls(raw)

        ios_key = _key_from_s3_uri(version.s3_json_url)
        unity_key = _key_from_s3_uri(_unity_layout_uri(version))

        ios_objects = _get_room_objects_with_model_ids(db, ios_key) if ios_key else []
        objects = _get_room_objects_with_model_ids(db, unity_key) if unity_key else []

        return _to_version_detail_response(
            version,
            usdz_empty_url=generate_presigned_url(empty_key) if empty_exists else None,
            model_urls=model_urls,
            model_usdz_urls=model_usdz_urls,
            objects=_attach_model_asset_urls(objects, model_urls, model_usdz_urls),
            ios_objects=_attach_model_asset_urls(ios_objects, model_urls, model_usdz_urls),
        )
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
                    glb_url=generate_presigned_url_for_uri(_catalog_glb_uri(model)),
                    width=float(model.width),
                    depth=float(model.depth),
                    height=float(model.height),
                )
                for model in models
            ]
        )
    finally:
        db.close()


# ── GET /rooms/{room_id} ──────────────────────────────────────────────────────

@router.get("/{room_id}", response_model=RoomSummaryResponse)
def get_room_by_id(room_id: int, current_user: User = Depends(get_current_user)):
    db = SessionLocal()
    try:
        room = db.query(Room).filter(Room.id == room_id, Room.user_id == current_user.id).first()
        if room is None:
            raise HTTPException(status_code=404, detail="room not found")
        return RoomSummaryResponse(room_id=room.id, status=room.status)
    finally:
        db.close()


# ── GET /rooms/{room_id}/versions ─────────────────────────────────────────────

@router.get("/{room_id}/versions", response_model=RoomVersionsResponse)
def get_room_versions(room_id: int, current_user: User = Depends(get_current_user)):
    db = SessionLocal()
    try:
        room = (
            db.query(Room)
            .options(joinedload(Room.versions))
            .filter(Room.id == room_id, Room.user_id == current_user.id)
            .first()
        )
        if room is None:
            raise HTTPException(status_code=404, detail="방을 찾을 수 없습니다.")

        versions = sorted(room.versions, key=lambda version: version.version_no)
        if not versions:
            raise HTTPException(status_code=404, detail="버전을 찾을 수 없습니다.")

        latest_version_no = max(version.version_no for version in versions)
        current_version_count = len(versions)
        return RoomVersionsResponse(
            room_id=room.id,
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
                    status=version.status,
                    created_at=version.created_at,
                    is_latest=version.version_no == latest_version_no,
                    can_delete=version.version_type == "USER_EDITED",
                )
                for version in versions
            ],
        )
    finally:
        db.close()


# ── GET /rooms/{room_id}/origin (다운로드) ──────────────────────────────────

@router.get("/{room_id}/origin", response_model=VersionAssetsResponse)
async def get_origin_assets(room_id: int, current_user: User = Depends(get_current_user)):
    db = SessionLocal()
    try:
        room = (
            db.query(Room)
            .options(joinedload(Room.user))
            .filter(Room.id == room_id, Room.user_id == current_user.id)
            .first()
        )
        owner_segment = _user_s3_segment(room.user) if room else None

        prefixes = await _resolve_prefixes(str(room_id), owner_segment)
        if prefixes is None:
            raise HTTPException(status_code=404, detail="방을 찾을 수 없습니다.")

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

        model_urls, model_usdz_urls = await _get_catalog_model_asset_urls(raw)

        # Unity와 iOS가 각자의 좌표계로 바로 장면을 조립할 수 있게 둘 다 제공한다.
        objects = []
        ios_objects = []
        if unity_data_exists:
            objects = _get_room_objects_with_model_ids(db, unity_data_key)
            objects = _attach_model_asset_urls(objects, model_urls, model_usdz_urls)
        ios_objects = _get_room_objects_with_model_ids(db, data_key)
        ios_objects = _attach_model_asset_urls(ios_objects, model_urls, model_usdz_urls)

        return VersionAssetsResponse(
            usdz_url=generate_presigned_url(full_key) if full_exists else None,
            usdz_empty_url=generate_presigned_url(empty_key) if empty_exists else None,
            glb_url=generate_presigned_url(glb_key) if glb_exists else None,
            data_url=generate_presigned_url(data_key),
            unity_data_url=generate_presigned_url(unity_data_key) if unity_data_exists else None,
            model_urls=model_urls,
            model_usdz_urls=model_usdz_urls,
            objects=objects,
            ios_objects=ios_objects,
        )
    finally:
        db.close()


# ── GET /rooms/{room_id}/optimized (다운로드) ───────────────────────────────

@router.get("/{room_id}/optimized", response_model=VersionAssetsResponse)
async def get_optimized_assets(room_id: int, current_user: User = Depends(get_current_user)):
    db = SessionLocal()
    try:
        room = (
            db.query(Room)
            .options(joinedload(Room.user))
            .filter(Room.id == room_id, Room.user_id == current_user.id)
            .first()
        )
        owner_segment = _user_s3_segment(room.user) if room else None
        prefixes = await _resolve_prefixes(str(room_id), owner_segment)
        if prefixes is None:
            raise HTTPException(status_code=404, detail="방을 찾을 수 없습니다.")

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

        model_urls, model_usdz_urls = await _get_catalog_model_asset_urls(raw)

        # Unity와 iOS가 각자의 좌표계로 바로 장면을 조립할 수 있게 둘 다 제공한다.
        objects = []
        ios_objects = []
        if unity_data_exists:
            objects = _get_room_objects_with_model_ids(db, unity_data_key)
            objects = _attach_model_asset_urls(objects, model_urls, model_usdz_urls)
        ios_objects = _get_room_objects_with_model_ids(db, data_key)
        ios_objects = _attach_model_asset_urls(ios_objects, model_urls, model_usdz_urls)

        return VersionAssetsResponse(
            usdz_url=generate_presigned_url(full_key) if full_exists else None,
            usdz_empty_url=generate_presigned_url(empty_key) if empty_exists else None,
            glb_url=generate_presigned_url(glb_key) if glb_exists else None,
            data_url=generate_presigned_url(data_key),
            unity_data_url=generate_presigned_url(unity_data_key) if unity_data_exists else None,
            model_urls=model_urls,
            model_usdz_urls=model_usdz_urls,
            objects=objects,
            ios_objects=ios_objects,
        )
    finally:
        db.close()
