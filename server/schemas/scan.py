from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


# ── iOS RoomPlan JSON 포맷 ────────────────────────────────────────────────────

class IOSObject(BaseModel):
    identifier: str
    category: str
    modelFileName: str | None = None
    center: list[float]
    dimensions: list[float]
    transform: list[list[float]]
    obbVertices: list[list[float]] | None = None
    frontVector: list[float] | None = None
    backVector: list[float] | None = None
    leftVector: list[float] | None = None
    rightVector: list[float] | None = None
    upVector: list[float] | None = None


class IOSFloor(BaseModel):
    center: list[float]
    dimensions: list[float]
    transform: list[list[float]]


class IOSWall(BaseModel):
    center: list[float]
    dimensions: list[float]
    transform: list[list[float]]


class IOSDoor(BaseModel):
    center: list[float] | None = None
    dimensions: list[float] | None = None
    transform: list[list[float]] | None = None


class IOSRoomScanData(BaseModel):
    coordinateSystem: str
    scannedAt: str
    objectCount: int
    objects: list[IOSObject]
    floors: list[IOSFloor]
    walls: list[IOSWall]
    doors: list[IOSDoor] = []


# ── 응답 모델 ─────────────────────────────────────────────────────────────────

class ScanUploadResponse(BaseModel):
    message: str
    room_id: int
    object_count: int
    uploaded_files: list[str]


# ── BE-A: Presigned 업로드 플로우 ─────────────────────────────────────────────

class PresignedUploadTarget(BaseModel):
    logical_name: str
    s3_key: str
    presigned_url: str
    content_type: str


class ScanUploadStartRequest(BaseModel):
    name: str | None = Field(default=None, max_length=100)
    include_room_usdz: bool = True
    include_room_empty_usdz: bool = False


class ScanUploadStartResponse(BaseModel):
    message: str
    room_id: int
    raw_prefix: str
    generated_prefix: str
    expires_in_seconds: int
    uploads: list[PresignedUploadTarget]


class ScanUploadCompleteRequest(BaseModel):
    uploaded_keys: list[str] = Field(default_factory=list)


class ScanUploadCompleteResponse(BaseModel):
    message: str
    room_id: int
    raw_prefix: str
    generated_prefix: str
    uploaded_keys: list[str]
    pipeline_started: bool
    execution_arn: str | None = None
    pipeline_input: dict[str, Any]


class ScanCancelResponse(BaseModel):
    """Response returned after an incomplete scan session is discarded."""

    room_id: int
    status: str
    deleted_s3_object_count: int


# ── BE-B: 조회 / 다운로드 ────────────────────────────────────────────────────

class VersionSummary(BaseModel):
    version_type: str          # "origin" | "optimized"
    created_at: datetime | None


class ScanDetailResponse(BaseModel):
    room_id: int
    created_at: datetime
    versions: list[VersionSummary]


class VersionAssetsResponse(BaseModel):
    usdz_url: str | None = None       # Room.usdz
    usdz_empty_url: str | None = None # Room_empty.usdz
    glb_url: str | None = None        # 변환된 3D 쉘 (output.glb)
    data_url: str                     # 가구 위치 JSON (origin or optimized)
    unity_data_url: str | None = None # Unity 정규화 좌표계 가구 위치 JSON
    model_urls: dict[str, str]        # { "chair_01.usdc": "catalog GLB presigned_url", ... }
    model_usdz_urls: dict[str, str] = Field(default_factory=dict)  # iOS/RealityKit 개별 가구 USDZ
    objects: list[dict[str, Any]] = Field(default_factory=list)      # Unity 좌표계 객체
    ios_objects: list[dict[str, Any]] = Field(default_factory=list)  # RoomPlan/iOS 좌표계 객체
