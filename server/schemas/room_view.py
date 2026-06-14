from __future__ import annotations

from datetime import datetime
from typing import Any
from pydantic import BaseModel


class RoomSummaryResponse(BaseModel):
    room_id: int
    confirm_code: str
    status: str


class RoomVersionItem(BaseModel):
    version_id: int
    version_type: str
    version_no: int
    version_name: str | None = None
    created_at: datetime | None = None
    is_latest: bool
    can_delete: bool


class RoomVersionsResponse(BaseModel):
    room_id: int
    confirm_code: str
    current_version_count: int
    max_version_count: int
    can_create_user_version: bool
    remaining_user_edit_slots: int
    versions: list[RoomVersionItem]


class FurnitureItemView(BaseModel):
    item_key: str
    model_key: str | None = None
    usdc_url: str | None = None
    glb_url: str | None = None
    pos: list[float]
    rot: list[float]
    scale: list[float]


class FurnitureCatalogItemResponse(BaseModel):
    model_key: str
    name: str | None = None
    furniture_type: str | None = None
    usdc_url: str | None = None
    glb_url: str | None = None
    width: float
    depth: float
    height: float


class FurnitureCatalogResponse(BaseModel):
    items: list[FurnitureCatalogItemResponse]


class VersionDetailResponse(BaseModel):
    version_id: int
    room_id: int
    confirm_code: str
    parent_version_id: int | None = None
    version_type: str
    version_no: int
    version_name: str | None = None
    created_at: datetime | None = None
    room_shell_url: str | None = None
    converted_glb_url: str | None = None
    layout_json_url: str | None = None
    unity_layout_json_url: str | None = None
    json_data: dict[str, Any] | None = None


class UserEditedVersionCreateRequest(BaseModel):
    parent_version_id: int
    version_name: str | None = None
    json_data: dict[str, Any] | None = None
    objects: list[dict[str, Any]]
    ios_objects: list[dict[str, Any]] | None = None


class UserEditedVersionCreateResponse(BaseModel):
    version_id: int
    room_id: int
    confirm_code: str
    parent_version_id: int
    version_type: str
    version_no: int
    version_name: str | None = None
    created_at: datetime | None = None
