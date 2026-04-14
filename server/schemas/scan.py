from typing import Any

from pydantic import BaseModel


class FurnitureItem(BaseModel):
    id: str
    type: str
    pos: list[float]   # [x, y, z]
    rot: list[float]   # [x, y, z] (Euler degrees)


class RoomScanRequest(BaseModel):
    room_id: str
    floor_polygon: list[list[float]]   # [[x, z], ...] 바닥 윤곽 좌표
    furniture: list[FurnitureItem]
    raw_metadata: dict[str, Any] | None = None   # iOS 원본 메타데이터 그대로 저장


class RoomScanResponse(BaseModel):
    message: str
    room_id: str
    s3_key: str
