from typing import Any

from pydantic import BaseModel


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
    confirm_code: str
    object_count: int
    s3_url: str
