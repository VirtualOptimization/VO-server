"""Furniture library endpoints."""

from __future__ import annotations

import re
from pathlib import Path
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from server.core.s3 import build_s3_uri, generate_presigned_url_for_uri
from server.schemas.furniture import (
    FurnitureModelCreateRequest,
    FurnitureModelDeleteResponse,
    FurnitureModelListResponse,
    FurnitureModelResponse,
)
from server.services.auth_service import get_user_by_access_token
from shared.db import SessionLocal
from shared.models.furniture_model import FurnitureModel
from shared.models.user import User

router = APIRouter()
bearer_scheme = HTTPBearer(auto_error=False)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Security(bearer_scheme),
    db: Session = Depends(get_db),
) -> User:
    if credentials is None or not credentials.credentials:
        raise HTTPException(status_code=401, detail="Authorization Bearer 토큰이 필요합니다.")
    return get_user_by_access_token(db, credentials.credentials)


def _safe_s3_segment(value: str | None, fallback: str) -> str:
    source = value or fallback
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", source).strip("._-")
    return safe or fallback


def _safe_filename(value: str) -> str:
    name = Path(value).name
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._-")
    return safe or "model.glb"


def _to_model_response(model: FurnitureModel) -> FurnitureModelResponse:
    return FurnitureModelResponse(
        model_id=model.id,
        model_key=model.model_key,
        name=model.name,
        status=model.status,
        glb_url=generate_presigned_url_for_uri(model.glb_url),
        width=model.width,
        depth=model.depth,
        height=model.height,
        created_at=model.created_at,
        updated_at=model.updated_at,
    )


@router.post("/models/register", response_model=FurnitureModelResponse, summary="내 가구 등록")
def create_furniture_model(
    request: FurnitureModelCreateRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    model_key = f"user_{current_user.id}_{uuid4().hex}"
    user_segment = _safe_s3_segment(current_user.login_id, f"user_{current_user.id}")
    model_filename = _safe_filename(request.model_filename)
    glb_key = f"{user_segment}/furniture/{model_key}/{model_filename}"

    model = FurnitureModel(
        user_id=current_user.id,
        model_key=model_key,
        name=request.name,
        furniture_type=None,
        status="READY",
        glb_url=build_s3_uri(glb_key),
        width=request.width,
        depth=request.depth,
        height=request.height,
    )

    db.add(model)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="이미 등록된 model_key입니다.") from exc

    db.refresh(model)
    return _to_model_response(model)


@router.get("/models", response_model=FurnitureModelListResponse, summary="내 가구 목록 조회")
def list_furniture_models(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    models = (
        db.query(FurnitureModel)
        .filter(
            FurnitureModel.user_id == current_user.id,
            FurnitureModel.status != "DELETED",
        )
        .order_by(FurnitureModel.created_at.desc(), FurnitureModel.id.desc())
        .all()
    )
    return FurnitureModelListResponse(
        models=[_to_model_response(model) for model in models]
    )


@router.delete("/models/{model_id}", response_model=FurnitureModelDeleteResponse, summary="내 가구 삭제")
def delete_furniture_model(
    model_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    model = db.get(FurnitureModel, model_id)
    if model is None or model.status == "DELETED":
        raise HTTPException(status_code=404, detail="가구 모델을 찾을 수 없습니다.")
    if model.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="삭제할 수 없는 가구 모델입니다.")

    model.status = "DELETED"
    db.commit()
    return FurnitureModelDeleteResponse(
        model_id=model.id,
        status=model.status,
        message="가구 모델이 삭제되었습니다.",
    )
