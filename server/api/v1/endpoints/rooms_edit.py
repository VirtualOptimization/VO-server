"""공간 편집 — USER_EDITED 버전 생성/삭제"""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException
from sqlalchemy import func

from server.core.s3 import get_json, parse_s3_uri, upload_json
from server.schemas.room_view import (
    UserEditedVersionCreateRequest,
    UserEditedVersionCreateResponse,
)
from server.services.transform.unity_roomplan import denormalize_roomplan_from_unity
from shared.db import SessionLocal
from shared.models.room import Room
from shared.models.version import Version

logger = logging.getLogger(__name__)
router = APIRouter()

MAX_VERSION_COUNT = 5
PATCHABLE_OBJECT_FIELDS = {
    "center",
    "transform",
    "obbVertices",
    "frontVector",
    "backVector",
    "leftVector",
    "rightVector",
    "upVector",
}


def _unity_layout_uri(version: Version) -> str | None:
    if not isinstance(version.json_data, dict):
        json_data = {}
    else:
        json_data = version.json_data

    unity_uri = (
        json_data.get("unity_layout_json")
        or json_data.get("unity_roomplan_optimized_json")
    )
    if unity_uri:
        return unity_uri

    if version.s3_json_url and version.s3_json_url.endswith("/room_data.json"):
        return version.s3_json_url.removesuffix("/room_data.json") + "/room_data.unity.json"

    return None


async def _load_json_from_s3_uri(uri: str | None, label: str) -> dict:
    parsed = parse_s3_uri(uri)
    if parsed is None:
        raise HTTPException(status_code=400, detail=f"{label} JSON 경로가 없습니다.")
    _, key = parsed
    return await get_json(key)


def _patch_objects(base_layout: dict, updates: list[dict], label: str) -> dict:
    if not updates:
        raise HTTPException(status_code=400, detail=f"{label} 수정 object 목록이 비어 있습니다.")

    base_objects = base_layout.get("objects")
    if not isinstance(base_objects, list):
        raise HTTPException(status_code=400, detail=f"{label} JSON에 objects 배열이 없습니다.")

    object_by_identifier = {
        item.get("identifier"): item
        for item in base_objects
        if isinstance(item, dict) and item.get("identifier")
    }

    for update in updates:
        identifier = update.get("identifier")
        if not identifier:
            raise HTTPException(status_code=400, detail=f"{label} 수정 object에 identifier가 필요합니다.")

        target = object_by_identifier.get(identifier)
        if target is None:
            raise HTTPException(
                status_code=400,
                detail=f"{label} JSON에서 identifier={identifier} object를 찾을 수 없습니다.",
            )

        for field in PATCHABLE_OBJECT_FIELDS:
            if field in update:
                target[field] = update[field]

    return base_layout


# ── POST /rooms/{confirm_code}/versions ──────────────────────────────────────
# Unity에서 수정한 가구 배치를 확인 코드 아래 새 USER_EDITED 버전으로 저장

@router.post(
    "/{confirm_code}/versions",
    response_model=UserEditedVersionCreateResponse,
    status_code=201,
)
async def create_user_edited_version(
    confirm_code: str,
    payload: UserEditedVersionCreateRequest,
):
    db = SessionLocal()
    try:
        room = (
            db.query(Room)
            .filter(Room.confirm_code == confirm_code)
            .with_for_update()
            .first()
        )
        if not room:
            raise HTTPException(status_code=404, detail="확인 코드를 찾을 수 없습니다.")

        parent_version = (
            db.query(Version)
            .filter(
                Version.id == payload.parent_version_id,
                Version.room_id == room.id,
            )
            .first()
        )
        if not parent_version:
            raise HTTPException(status_code=404, detail="부모 버전을 찾을 수 없습니다.")

        current_version_count = (
            db.query(func.count(Version.id))
            .filter(Version.room_id == room.id)
            .scalar()
        )
        if current_version_count >= MAX_VERSION_COUNT:
            raise HTTPException(
                status_code=409,
                detail={
                    "message": "저장 가능한 버전 개수를 초과했습니다. USER_EDITED 버전을 삭제한 뒤 다시 저장해주세요.",
                    "current_version_count": current_version_count,
                    "max_version_count": MAX_VERSION_COUNT,
                },
            )

        next_version_no = (
            db.query(func.coalesce(func.max(Version.version_no), 0) + 1)
            .filter(Version.room_id == room.id)
            .scalar()
        )
        version_name = payload.version_name or f"Unity Edit {next_version_no}"
        unity_layout = await _load_json_from_s3_uri(_unity_layout_uri(parent_version), "Unity")
        ios_layout = await _load_json_from_s3_uri(parent_version.s3_json_url, "iOS")

        unity_layout = _patch_objects(unity_layout, payload.objects, "Unity")
        if payload.ios_objects is not None:
            ios_layout = _patch_objects(ios_layout, payload.ios_objects, "iOS")
        else:
            ios_layout = denormalize_roomplan_from_unity(unity_layout)

        edit_prefix = f"scans/{confirm_code}/user_edits/version_{next_version_no}"
        ios_key = f"{edit_prefix}/layout.roomplan.json"
        unity_key = f"{edit_prefix}/layout.unity.json"
        ios_s3_url = await upload_json(ios_key, ios_layout)
        unity_s3_url = await upload_json(unity_key, unity_layout)

        version = Version(
            room_id=room.id,
            parent_version_id=parent_version.id,
            version_type="USER_EDITED",
            version_no=next_version_no,
            version_name=version_name,
            s3_json_url=ios_s3_url,
            converted_glb_url=parent_version.converted_glb_url,
            json_data={
                **(payload.json_data or {}),
                "source": "unity_edit",
                "parent_version_id": parent_version.id,
                "layout_json": ios_s3_url,
                "unity_layout_json": unity_s3_url,
            },
        )
        db.add(version)

        db.commit()
        db.refresh(version)

        return UserEditedVersionCreateResponse(
            version_id=version.id,
            room_id=room.id,
            confirm_code=room.confirm_code,
            parent_version_id=parent_version.id,
            version_type=version.version_type,
            version_no=version.version_no,
            version_name=version.version_name,
            created_at=version.created_at,
        )

    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        logger.error(f"Failed to create USER_EDITED version for {confirm_code}: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        db.close()


# ── DELETE /rooms/{confirm_code}/versions/{version_id} ────────────────────────
# origin(ORIGINAL) / optimized(OPTIMIZED) 는 삭제 불가
# USER_EDITED 버전만 삭제 가능

@router.delete("/{confirm_code}/versions/{version_id}", status_code=204)
def delete_user_version(confirm_code: str, version_id: int):
    db = SessionLocal()
    try:
        room = db.query(Room).filter(Room.confirm_code == confirm_code).first()
        if not room:
            raise HTTPException(status_code=404, detail="확인 코드를 찾을 수 없습니다.")

        version = db.query(Version).filter(
            Version.id == version_id,
            Version.room_id == room.id,
        ).first()

        if not version:
            raise HTTPException(status_code=404, detail="버전을 찾을 수 없습니다.")

        if version.version_type != "USER_EDITED":
            raise HTTPException(
                status_code=403,
                detail="origin 및 optimized 버전은 삭제할 수 없습니다. USER_EDITED 버전만 삭제 가능합니다.",
            )

        db.delete(version)
        db.commit()

    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        logger.error(f"Failed to delete version {version_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        db.close()
