from __future__ import annotations

from fastapi import APIRouter, HTTPException
from sqlalchemy.orm import joinedload

from shared.db import SessionLocal
from shared.models.room import Room
from shared.models.version import Version
from shared.models.furniture_item import FurnitureItem
from server.schemas.room_view import (
    RoomSummaryResponse,
    RoomVersionsResponse,
    RoomVersionItem,
    VersionDetailResponse,
    FurnitureItemView,
)

router = APIRouter()


@router.get("/{confirm_code}", response_model=RoomSummaryResponse, summary="""
    코드가 유효한지 체크하고, COMPLETED가 될 때까지 로딩창 표시
    """)
def get_room_by_confirm_code(confirm_code: str):
    db = SessionLocal()
    try:
        room = db.query(Room).filter(Room.confirm_code == confirm_code).first()
        if room is None:
            raise HTTPException(status_code=404, detail="room not found")

        return RoomSummaryResponse(
            room_id=room.id,
            confirm_code=room.confirm_code,
            status=room.status,
        )
    finally:
        db.close()


@router.get("/{confirm_code}/versions", response_model=RoomVersionsResponse, summary="""
    해당 룸에 존재하는 모든 레이아웃 버전(원본, AI 최적화, 유저 수정본) 리스트 조회
    """)
def get_room_versions(confirm_code: str):
    db = SessionLocal()
    try:
        room = db.query(Room).filter(Room.confirm_code == confirm_code).first()
        if room is None:
            raise HTTPException(status_code=404, detail="room not found")

        versions = (
            db.query(Version)
            .filter(Version.room_id == room.id)
            .order_by(Version.version_no.asc(), Version.id.asc())
            .all()
        )

        if not versions:
            return RoomVersionsResponse(room_id=room.id, confirm_code=room.confirm_code, versions=[])

        latest_version_no = max(v.version_no for v in versions) if versions else 0

        return RoomVersionsResponse(
            room_id=room.id,
            confirm_code=room.confirm_code,
            versions=[
                RoomVersionItem(
                    version_id=v.id,
                    version_type=v.version_type,
                    version_no=v.version_no,
                    created_at=None,  # DB에 컬럼 없어서 우선 None
                    is_latest=(v.version_no == latest_version_no),
                )
                for v in versions
            ],
        )
    finally:
        db.close()


@router.get("/versions/{version_id}", response_model=VersionDetailResponse, summary="""
    선택한 레이아웃 버전의 가구 배치(x, y, z 좌표, 회전값) 및 FBX 파일 주소 통째로 조회
    """)
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

        furniture_items = []
        for item in version.furniture_items:
            furniture_items.append(
                FurnitureItemView(
                    item_key=item.item_key,
                    model_key=item.model.model_key if item.model else None,
                    usdc_url=item.model.usdc_url if item.model else None,
                    pos=[float(item.pos_x), float(item.pos_y), float(item.pos_z)],
                    rot=[float(item.rot_x), float(item.rot_y), float(item.rot_z)],
                    scale=[float(item.scale_x), float(item.scale_y), float(item.scale_z)],
                )
            )

        return VersionDetailResponse(
            version_id=version.id,
            room_id=version.room_id,
            confirm_code=version.room.confirm_code if version.room else "",
            version_type=version.version_type,
            version_no=version.version_no,
            room_shell_url=version.room.room_shell_usdc_url if version.room else None,
            converted_fbx_url=version.converted_fbx_url,
            layout_json_url=version.s3_json_url,
            json_data=version.json_data,
            furniture_items=furniture_items,
        )
    finally:
        db.close()
