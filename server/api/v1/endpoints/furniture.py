"""Furniture library endpoints."""

from __future__ import annotations

import subprocess
from urllib.parse import unquote, urlparse
from uuid import uuid4

from botocore.exceptions import ClientError
from fastapi import APIRouter, Depends, HTTPException, Security
from fastapi.responses import StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from server.api.v1.endpoints.rooms_common import _safe_s3_segment
from server.core.s3 import (
    build_s3_uri,
    generate_presigned_put_url,
    generate_presigned_url_for_uri,
    get_s3_client,
    object_exists,
    parse_s3_uri,
)
from server.schemas.furniture import (
    FurnitureModelCreateRequest,
    FurnitureModelDeleteResponse,
    FurnitureModelListResponse,
    FurnitureModelResponse,
    FurnitureModelUsdzConversionResponse,
    FurnitureModelUsdzUpdateRequest,
    TexturePresetListResponse,
    TexturePresetResponse,
)
from server.services.auth_service import get_user_by_access_token
from server.services.furniture_conversion import (
    FurnitureConversionError,
    convert_furniture_glb_to_usdz,
    convert_furniture_usdc_to_glb,
)
from server.core.config import settings
from shared.db import SessionLocal
from shared.models.furniture_model import FurnitureModel
from shared.models.texture_preset import TexturePreset
from shared.models.user import User

router = APIRouter()
bearer_scheme = HTTPBearer(auto_error=False)
USDC_CONTENT_TYPE = "application/octet-stream"


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


def _to_model_response(
    model: FurnitureModel,
    upload_url: str | None = None,
    upload_s3_key: str | None = None,
) -> FurnitureModelResponse:
    return FurnitureModelResponse(
        model_id=model.id,
        model_key=model.model_key,
        name=model.name,
        status=model.status,
        glb_url=generate_presigned_url_for_uri(model.glb_url) if model.status == "READY" else None,
        usdz_url=generate_presigned_url_for_uri(model.usdz_url) if model.status == "READY" else None,
        upload_url=upload_url,
        upload_content_type=USDC_CONTENT_TYPE if upload_url else None,
        upload_s3_key=upload_s3_key,
        width=model.width,
        depth=model.depth,
        height=model.height,
        created_at=model.created_at,
        updated_at=model.updated_at,
    )


def _furniture_key_prefix(current_user: User, model_key: str) -> str:
    user_segment = _safe_s3_segment(current_user.login_id, f"user_{current_user.id}")
    return f"{user_segment}/furniture/{model_key}"


def _source_usdc_key(current_user: User, model_key: str) -> str:
    return f"{_furniture_key_prefix(current_user, model_key)}.usdc"


def _output_glb_key(current_user: User, model_key: str) -> str:
    return f"{_furniture_key_prefix(current_user, model_key)}.glb"


def _output_usdz_key(current_user: User, model_key: str) -> str:
    return f"{_furniture_key_prefix(current_user, model_key)}.usdz"


def _key_from_s3_uri(uri: str | None) -> str | None:
    parsed = parse_s3_uri(uri)
    if parsed is None:
        return None
    return parsed[1]


def _normalize_s3_key(value: str) -> str:
    raw = value.strip()
    if not raw:
        raise ValueError("S3 경로가 비어 있습니다.")

    parsed_s3 = parse_s3_uri(raw)
    if parsed_s3 is not None:
        bucket, key = parsed_s3
        if bucket != settings.s3_bucket_name:
            raise ValueError("현재 S3 버킷의 파일만 사용할 수 있습니다.")
        return key

    parsed_url = urlparse(raw)
    if parsed_url.scheme in {"http", "https"}:
        host = parsed_url.netloc
        path = unquote(parsed_url.path.lstrip("/"))

        virtual_host_prefix = f"{settings.s3_bucket_name}."
        if host.startswith(virtual_host_prefix):
            return path

        path_style_prefix = f"{settings.s3_bucket_name}/"
        if path.startswith(path_style_prefix):
            return path.removeprefix(path_style_prefix)

        raise ValueError("현재 S3 버킷의 URL만 사용할 수 있습니다.")

    return raw.lstrip("/")


def _get_owned_furniture_model(db: Session, model_id: int, current_user: User) -> FurnitureModel:
    model = db.get(FurnitureModel, model_id)
    if model is None or model.status == "DELETED":
        raise HTTPException(status_code=404, detail="가구 모델을 찾을 수 없습니다.")
    if model.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="접근할 수 없는 가구 모델입니다.")
    return model


def _to_texture_preset_response(preset: TexturePreset) -> TexturePresetResponse:
    return TexturePresetResponse(
        preset_id=preset.id,
        preset_key=preset.preset_key,
        name=preset.name,
        texture_s3_key=preset.texture_s3_key,
        texture_url=generate_presigned_url_for_uri(build_s3_uri(preset.texture_s3_key)),
        created_at=preset.created_at,
        updated_at=preset.updated_at,
    )


def _stream_s3_body(body, chunk_size: int = 1024 * 1024):
    try:
        while True:
            chunk = body.read(chunk_size)
            if not chunk:
                break
            yield chunk
    finally:
        body.close()


def _stream_model_glb(model: FurnitureModel):
    if model.status != "READY":
        raise HTTPException(status_code=400, detail="GLB 변환이 완료된 가구만 조회할 수 있습니다.")

    parsed = parse_s3_uri(model.glb_url)
    if parsed is None:
        raise HTTPException(status_code=400, detail="가구 GLB 경로가 올바르지 않습니다.")

    bucket, key = parsed
    try:
        response = get_s3_client().get_object(Bucket=bucket, Key=key)
    except ClientError as exc:
        error_code = exc.response.get("Error", {}).get("Code")
        if error_code in {"404", "NoSuchKey", "NotFound"}:
            raise HTTPException(status_code=404, detail="가구 GLB 파일을 찾을 수 없습니다.") from exc
        raise HTTPException(status_code=500, detail="가구 GLB 파일 조회에 실패했습니다.") from exc

    headers = {
        "Content-Disposition": f'inline; filename="{model.model_key}.glb"',
    }
    content_length = response.get("ContentLength")
    if content_length is not None:
        headers["Content-Length"] = str(content_length)

    return StreamingResponse(
        _stream_s3_body(response["Body"]),
        media_type="model/gltf-binary",
        headers=headers,
    )


@router.post("/models/register", response_model=FurnitureModelResponse, summary="내 가구 등록")
async def create_furniture_model(
    request: FurnitureModelCreateRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    model_key = uuid4().hex
    source_key = _source_usdc_key(current_user, model_key)
    glb_key = _output_glb_key(current_user, model_key)

    model = FurnitureModel(
        user_id=current_user.id,
        model_key=model_key,
        name=request.name,
        furniture_type=None,
        status="UPLOADING",
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
    upload_url = await generate_presigned_put_url(source_key, USDC_CONTENT_TYPE)
    return _to_model_response(model, upload_url=upload_url, upload_s3_key=source_key)


@router.get(
    "/texture-presets",
    response_model=TexturePresetListResponse,
    summary="기본 가구 텍스처 preset 목록 조회",
)
def list_texture_presets(
    _current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    presets = db.query(TexturePreset).order_by(TexturePreset.id.asc()).all()
    return TexturePresetListResponse(presets=[_to_texture_preset_response(preset) for preset in presets])


@router.post(
    "/models/{model_id}/complete",
    response_model=FurnitureModelResponse,
    summary="내 가구 업로드 완료 및 GLB 변환",
)
async def complete_furniture_model_upload(
    model_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    model = db.get(FurnitureModel, model_id)
    if model is None or model.status == "DELETED":
        raise HTTPException(status_code=404, detail="가구 모델을 찾을 수 없습니다.")
    if model.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="변환할 수 없는 가구 모델입니다.")
    if model.status == "READY":
        return _to_model_response(model)

    source_key = _source_usdc_key(current_user, model.model_key)
    output_key = _key_from_s3_uri(model.glb_url) or _output_glb_key(current_user, model.model_key)

    if not await object_exists(source_key):
        raise HTTPException(status_code=400, detail="업로드된 USDC 파일을 찾을 수 없습니다.")

    model.status = "PROCESSING"
    db.commit()

    try:
        await convert_furniture_usdc_to_glb(source_key, output_key)
    except (FurnitureConversionError, ClientError, OSError, ValueError) as exc:
        model.status = "FAILED"
        db.commit()
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    model.status = "READY"
    model.glb_url = build_s3_uri(output_key)
    db.commit()
    db.refresh(model)
    return _to_model_response(model)


@router.get(
    "/models/{model_id}/glb",
    summary="내 가구 GLB 파일 스트리밍",
)
def stream_furniture_model_glb(
    model_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    model = _get_owned_furniture_model(db, model_id, current_user)
    return _stream_model_glb(model)


@router.post(
    "/models/{model_id}/convert-usdz",
    response_model=FurnitureModelUsdzConversionResponse,
    summary="내 가구 GLB를 USDZ로 변환",
)
async def convert_furniture_model_to_usdz(
    model_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    model = db.get(FurnitureModel, model_id)
    if model is None or model.status == "DELETED":
        raise HTTPException(status_code=404, detail="가구 모델을 찾을 수 없습니다.")
    if model.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="변환할 수 없는 가구 모델입니다.")
    if model.status != "READY":
        raise HTTPException(status_code=400, detail="GLB 변환이 완료된 가구만 USDZ로 변환할 수 있습니다.")

    source_key = _key_from_s3_uri(model.glb_url)
    if not source_key:
        raise HTTPException(status_code=400, detail="변환할 GLB 경로가 없습니다.")

    output_key = _output_usdz_key(current_user, model.model_key)

    if not await object_exists(source_key):
        raise HTTPException(status_code=400, detail="변환할 GLB 파일을 찾을 수 없습니다.")

    try:
        await convert_furniture_glb_to_usdz(source_key, output_key)
    except (subprocess.SubprocessError, TimeoutError, ClientError, OSError, ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=500, detail=f"가구 USDZ 변환에 실패했습니다: {exc}") from exc

    model.usdz_url = build_s3_uri(output_key)
    db.commit()
    db.refresh(model)

    return FurnitureModelUsdzConversionResponse(
        model_id=model.id,
        model_key=model.model_key,
        status=model.status,
        glb_url=generate_presigned_url_for_uri(model.glb_url),
        usdz_url=generate_presigned_url_for_uri(model.usdz_url),
        usdz_s3_key=output_key,
    )


@router.patch(
    "/models/{model_id}/usdz",
    response_model=FurnitureModelResponse,
    summary="내 가구 USDZ 경로 갱신",
)
async def update_furniture_model_usdz_url(
    model_id: int,
    request: FurnitureModelUsdzUpdateRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    model = db.get(FurnitureModel, model_id)
    if model is None or model.status == "DELETED":
        raise HTTPException(status_code=404, detail="가구 모델을 찾을 수 없습니다.")
    if model.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="수정할 수 없는 가구 모델입니다.")

    try:
        usdz_key = _normalize_s3_key(request.usdz_url)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if not usdz_key.endswith(".usdz"):
        raise HTTPException(status_code=400, detail="USDZ 파일 경로만 등록할 수 있습니다.")
    if not await object_exists(usdz_key):
        raise HTTPException(status_code=400, detail="등록할 USDZ 파일을 찾을 수 없습니다.")

    model.usdz_url = build_s3_uri(usdz_key)
    db.commit()
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
