"""공간 편집 — USER_EDITED 버전 생성/삭제"""
from __future__ import annotations

import logging
import math

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func

from server.api.v1.deps import get_current_user
from server.api.v1.endpoints.rooms_common import _scan_root, _user_s3_segment
from server.core.s3 import delete_objects, get_json, list_keys, parse_s3_uri, upload_json
from server.schemas.room_view import (
    UserEditedVersionCreateRequest,
    UserEditedVersionCreateResponse,
)
from server.services.transform.unity_roomplan import (
    denormalize_roomplan_from_unity,
    fix_unity_transforms_from_rotation,
    normalize_roomplan_for_ios_view,
    normalize_roomplan_for_unity,
)
from shared.db import SessionLocal
from shared.models.room import Room
from shared.models.user import User
from shared.models.version import Version

logger = logging.getLogger(__name__)
router = APIRouter()

MAX_VERSION_COUNT = 5
PATCHABLE_OBJECT_FIELDS = {
    "center",
    "rotation",
    "transform",
    "frontVector",
    "backVector",
    "leftVector",
    "rightVector",
    "upVector",
}


def _round6(value: float) -> float:
    return round(float(value), 6)


def _unity_rotation_from_transform(transform: list[list[float]]) -> list[float]:
    # Catalog GLB furniture faces local -Z in Unity, while RoomPlan transform row 2 is the back axis.
    yaw = math.atan2(float(transform[2][0]), float(transform[2][2]))
    return [0.0, _round6(yaw), 0.0]


def _roomplan_rotation_from_transform(transform: list[list[float]]) -> list[float]:
    yaw = math.atan2(float(transform[0][2]), float(transform[0][0]))
    return [0.0, _round6(yaw), 0.0]


def _yaw_from_rotation(rotation: list) -> float:
    if len(rotation) >= 3:
        return float(rotation[1])
    if len(rotation) >= 1:
        return float(rotation[0])
    raise HTTPException(status_code=400, detail="rotation은 [x, y, z] 또는 [yaw] 형식이어야 합니다.")


def _apply_yaw_to_transform(item: dict, yaw: float, coordinate_space: str) -> None:
    transform = item.get("transform")
    if not isinstance(transform, list) or len(transform) < 4:
        center = item.get("center") or [0.0, 0.0, 0.0]
        transform = [
            [1.0, 0.0, 0.0, 0.0],
            [0.0, 1.0, 0.0, 0.0],
            [0.0, 0.0, 1.0, 0.0],
            [_round6(center[0]), _round6(center[1]), _round6(center[2]), 1.0],
        ]

    c = math.cos(yaw)
    s = math.sin(yaw)
    if coordinate_space == "roomplan":
        transform[0][0:3] = [_round6(c), 0.0, _round6(s)]
        transform[1][0:3] = [0.0, 1.0, 0.0]
        transform[2][0:3] = [_round6(-s), 0.0, _round6(c)]
        item["transform"] = transform
        return

    transform[0][0:3] = [_round6(c), 0.0, _round6(-s)]
    transform[1][0:3] = [0.0, 1.0, 0.0]
    transform[2][0:3] = [_round6(s), 0.0, _round6(c)]
    item["transform"] = transform


def _center_y(item: dict) -> float:
    center = item.get("center")
    if isinstance(center, list) and len(center) >= 2:
        center_y = float(center[1])
        obb_min_y = _obb_min_y(item)
        if obb_min_y is not None and obb_min_y < -0.01:
            return center_y - obb_min_y
        return center_y

    transform = item.get("transform")
    if isinstance(transform, list) and len(transform) >= 4 and len(transform[3]) >= 2:
        center_y = float(transform[3][1])
        obb_min_y = _obb_min_y(item)
        if obb_min_y is not None and obb_min_y < -0.01:
            return center_y - obb_min_y
        return center_y

    return 0.0


def _obb_min_y(item: dict) -> float | None:
    vertices = item.get("obbVertices")
    if not isinstance(vertices, list):
        return None

    y_values = [
        float(vertex[1])
        for vertex in vertices
        if isinstance(vertex, list) and len(vertex) >= 2 and isinstance(vertex[1], (int, float))
    ]
    if not y_values:
        return None

    return min(y_values)


def _vec3(row: list) -> list[float]:
    return [float(row[0]), float(row[1]), float(row[2])]


def _neg(vector: list[float]) -> list[float]:
    return [_round6(-value) for value in vector]


def _rounded(vector: list[float]) -> list[float]:
    return [_round6(value) for value in vector]


def _rounded_transform(transform: list[list[float]]) -> list[list[float]]:
    rounded = []
    for row in transform:
        rounded.append([_round6(value) if isinstance(value, (int, float)) else value for value in row])
    return rounded


def _translation_norm(values: list[float]) -> float:
    return math.sqrt(sum(float(value) * float(value) for value in values))


def _normalized_xz_axis(vector: list[float]) -> list[float] | None:
    length = math.sqrt(float(vector[0]) * float(vector[0]) + float(vector[2]) * float(vector[2]))
    if length < 1e-6:
        return None
    return [_round6(float(vector[0]) / length), 0.0, _round6(float(vector[2]) / length)]


def _canonicalize_roomplan_transform(transform: list[list[float]]) -> list[list[float]]:
    back = _normalized_xz_axis(transform[2])
    if back is None:
        return transform

    right = [back[2], 0.0, _round6(-back[0])]
    transform[0][0:3] = right
    transform[1][0:3] = [0.0, 1.0, 0.0]
    transform[2][0:3] = back
    return transform


def _normalize_incoming_transform(transform: list[list[float]], coordinate_space: str) -> list[list[float]]:
    rounded = _rounded_transform(transform)
    if coordinate_space != "roomplan":
        return rounded

    row_translation = rounded[3][0:3]
    column_translation = [rounded[0][3], rounded[1][3], rounded[2][3]]
    if _translation_norm(row_translation) < 1e-6 and _translation_norm(column_translation) > 1e-6:
        rounded = [[rounded[col][row] for col in range(4)] for row in range(4)]

    return _canonicalize_roomplan_transform(rounded)


def _sync_vectors_from_transform(item: dict, coordinate_space: str) -> None:
    transform = item.get("transform")
    if not isinstance(transform, list) or len(transform) < 4:
        return
    if any(not isinstance(transform[row], list) or len(transform[row]) < 3 for row in range(3)):
        return

    right = _vec3(transform[0])
    up = _vec3(transform[1])
    back = _vec3(transform[2])
    item["rightVector"] = _rounded(right)
    item["leftVector"] = _neg(right)
    item["upVector"] = _rounded(up)
    item["backVector"] = _rounded(back)
    item["frontVector"] = _neg(back)
    if coordinate_space == "roomplan":
        item["rotation"] = _roomplan_rotation_from_transform(transform)
    else:
        item["rotation"] = _unity_rotation_from_transform(transform)


def _sync_obb_vertices(item: dict) -> None:
    center = item.get("center")
    dimensions = item.get("dimensions")
    transform = item.get("transform")
    if not (
        isinstance(center, list) and len(center) >= 3
        and isinstance(dimensions, list) and len(dimensions) >= 3
        and isinstance(transform, list) and len(transform) >= 3
    ):
        return
    if any(not isinstance(transform[row], list) or len(transform[row]) < 3 for row in range(3)):
        return

    hx, hy, hz = float(dimensions[0]) / 2.0, float(dimensions[1]) / 2.0, float(dimensions[2]) / 2.0
    center_v = _vec3(center)
    right = _vec3(transform[0])
    up = _vec3(transform[1])
    back = _vec3(transform[2])

    vertices = []
    for sx in (1.0, -1.0):
        for sy in (-1.0, 1.0):
            for sz in (1.0, -1.0):
                point = [
                    center_v[axis] + right[axis] * hx * sx + up[axis] * hy * sy + back[axis] * hz * sz
                    for axis in range(3)
                ]
                vertices.append(_rounded(point))
    item["obbVertices"] = vertices


def _apply_pose_update(target: dict, update: dict, coordinate_space: str) -> None:
    preserved_y = _center_y(target)
    preserved_rotation = target.get("rotation")
    preserved_yaw = (
        _yaw_from_rotation(preserved_rotation)
        if isinstance(preserved_rotation, list) and preserved_rotation
        else None
    )

    if "center" in update:
        center = update["center"]
        if not isinstance(center, list) or len(center) < 3:
            raise HTTPException(status_code=400, detail="center는 [x, y, z] 형식이어야 합니다.")
        target["center"] = [_round6(center[0]), _round6(preserved_y), _round6(center[2])]

    if "transform" in update:
        transform = update["transform"]
        if (
            not isinstance(transform, list)
            or len(transform) < 4
            or any(not isinstance(row, list) or len(row) < 4 for row in transform[:4])
        ):
            raise HTTPException(status_code=400, detail="transform은 4x4 행렬 형식이어야 합니다.")
        target["transform"] = _normalize_incoming_transform(transform, coordinate_space)
        if "center" not in update:
            target["center"] = [
                _round6(target["transform"][3][0]),
                _round6(preserved_y),
                _round6(target["transform"][3][2]),
            ]

    if "rotation" in update:
        rotation = update["rotation"]
        if not isinstance(rotation, list):
            raise HTTPException(status_code=400, detail="rotation은 배열 형식이어야 합니다.")
        yaw = _yaw_from_rotation(rotation)
        target["rotation"] = [0.0, _round6(yaw), 0.0]
        _apply_yaw_to_transform(target, yaw, coordinate_space)

    if "transform" in update or "center" in update or "rotation" in update:
        transform = target.get("transform")
        if isinstance(transform, list) and len(transform) >= 4:
            transform[3][0:3] = target["center"]
        if "rotation" not in update and "transform" not in update and preserved_yaw is not None:
            target["rotation"] = [0.0, _round6(preserved_yaw), 0.0]
            _apply_yaw_to_transform(target, preserved_yaw, coordinate_space)
        _sync_vectors_from_transform(target, coordinate_space)
        _sync_obb_vertices(target)


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


def _patch_objects(base_layout: dict, updates: list[dict], label: str, coordinate_space: str) -> dict:
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

        _apply_pose_update(target, update, coordinate_space)

    return base_layout


def _version_s3_prefix(version: Version) -> str | None:
    parsed = parse_s3_uri(version.s3_json_url)
    if parsed is None:
        return None

    _, key = parsed
    marker = "/user_edits/"
    if marker not in key:
        return None

    prefix, filename = key.rsplit("/", 1)
    if not filename:
        return None
    return f"{prefix}/"


# ── POST /rooms/{room_id}/versions ───────────────────────────────────────────
# Unity에서 수정한 가구 배치를 room_id 아래 새 USER_EDITED 버전으로 저장

@router.post(
    "/{room_id}/versions",
    response_model=UserEditedVersionCreateResponse,
    status_code=201,
)
async def create_user_edited_version(
    room_id: int,
    payload: UserEditedVersionCreateRequest,
    current_user: User = Depends(get_current_user),
):
    db = SessionLocal()
    try:
        room = (
            db.query(Room)
            .filter(Room.id == room_id, Room.user_id == current_user.id)
            .with_for_update()
            .first()
        )
        if not room:
            raise HTTPException(status_code=404, detail="방을 찾을 수 없습니다.")

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
        if not payload.objects and not payload.ios_objects:
            raise HTTPException(status_code=400, detail="objects 또는 ios_objects 중 하나는 필요합니다.")

        version_name = payload.version_name or f"User Edit {next_version_no}"
        ios_layout = await _load_json_from_s3_uri(parent_version.s3_json_url, "iOS")

        if payload.ios_objects is not None:
            ios_layout = normalize_roomplan_for_ios_view(
                _patch_objects(ios_layout, payload.ios_objects, "iOS", "roomplan")
            )
            if payload.objects:
                unity_layout = await _load_json_from_s3_uri(_unity_layout_uri(parent_version), "Unity")
                unity_layout = _patch_objects(unity_layout, payload.objects, "Unity", "unity")
                unity_layout = fix_unity_transforms_from_rotation(unity_layout)
            else:
                unity_layout = normalize_roomplan_for_unity(ios_layout)
                unity_layout = fix_unity_transforms_from_rotation(unity_layout)
        else:
            unity_layout = await _load_json_from_s3_uri(_unity_layout_uri(parent_version), "Unity")
            unity_layout = _patch_objects(unity_layout, payload.objects, "Unity", "unity")
            unity_layout = fix_unity_transforms_from_rotation(unity_layout)
            ios_layout = denormalize_roomplan_from_unity(unity_layout)
            ios_layout = normalize_roomplan_for_ios_view(ios_layout)

        owner_segment = _user_s3_segment(room.user)
        edit_prefix = f"{_scan_root(str(room.id), owner_segment)}/user_edits/version_{next_version_no}"
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
        logger.error(f"Failed to create USER_EDITED version for room_id={room_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        db.close()


# ── DELETE /rooms/{room_id}/versions/{version_id} ─────────────────────────────
# origin(ORIGINAL) / optimized(OPTIMIZED) 는 삭제 불가
# USER_EDITED 버전만 삭제 가능

@router.delete("/{room_id}/versions/{version_id}", status_code=204)
def delete_user_version(
    room_id: int,
    version_id: int,
    current_user: User = Depends(get_current_user),
):
    db = SessionLocal()
    try:
        room = db.query(Room).filter(Room.id == room_id, Room.user_id == current_user.id).first()
        if not room:
            raise HTTPException(status_code=404, detail="방을 찾을 수 없습니다.")

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

        s3_prefix = _version_s3_prefix(version)
        if s3_prefix:
            delete_objects(list_keys(s3_prefix))

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
