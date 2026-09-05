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
    usdz_url: str | None
    usdz_s3_key: str
