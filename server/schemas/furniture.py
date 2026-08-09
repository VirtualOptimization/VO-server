from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class FurnitureModelCreateRequest(BaseModel):
    name: str | None = Field(default=None, max_length=100)
    model_filename: str = Field(default="model.usdc", min_length=1, max_length=255)
    width: float = Field(..., gt=0)
    depth: float = Field(..., gt=0)
    height: float = Field(..., gt=0)


class FurnitureModelResponse(BaseModel):
    model_id: int
    model_key: str
    name: str | None
    status: str
    glb_url: str | None
    usdz_url: str | None
    upload_url: str | None = None
    upload_content_type: str | None = None
    upload_s3_key: str | None = None
    width: float
    depth: float
    height: float
    created_at: datetime
    updated_at: datetime


class FurnitureModelListResponse(BaseModel):
    models: list[FurnitureModelResponse]


class FurnitureModelDeleteResponse(BaseModel):
    model_id: int
    status: str
    message: str


class FurnitureModelUsdzUpdateRequest(BaseModel):
    usdz_url: str = Field(..., min_length=1)


class FurnitureModelUsdzConversionResponse(BaseModel):
    model_id: int
    model_key: str
    status: str
    glb_url: str | None
    usdz_url: str
    usdz_s3_key: str


class FurnitureMaterialAssetRegisterRequest(BaseModel):
    room_id: int
    version_id: int
    furniture_instance_id: str = Field(..., min_length=1, max_length=100)
    material_preset_id: str = Field(..., min_length=1, max_length=100)
    material_name: str | None = Field(default=None, max_length=100)


class FurnitureMaterialAssetCompleteRequest(BaseModel):
    room_id: int
    version_id: int
    furniture_instance_id: str = Field(..., min_length=1, max_length=100)
    material_preset_id: str = Field(..., min_length=1, max_length=100)


class FurnitureMaterialAssetResponse(BaseModel):
    material_asset_key: str
    status: str
    base_model_id: int
    model_key: str
    material_model_key: str
    room_id: int
    version_id: int
    furniture_instance_id: str
    material_preset_id: str
    material_name: str | None
    glb_url: str | None
    usdz_url: str | None
    upload_url: str | None = None
    upload_content_type: str | None = None
    upload_s3_key: str | None = None
    glb_s3_key: str
    usdz_s3_key: str
