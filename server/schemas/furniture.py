from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class FurnitureModelCreateRequest(BaseModel):
    name: str | None = Field(default=None, max_length=100)
    model_filename: str = Field(default="model.glb", min_length=1, max_length=255)
    width: float = Field(..., gt=0)
    depth: float = Field(..., gt=0)
    height: float = Field(..., gt=0)


class FurnitureModelResponse(BaseModel):
    model_id: int
    model_key: str
    name: str | None
    status: str
    glb_url: str | None
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
