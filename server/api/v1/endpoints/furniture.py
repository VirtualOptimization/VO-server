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
    FurnitureMaterialAssetCompleteRequest,
    FurnitureMaterialAssetRegisterRequest,
    FurnitureMaterialAssetResponse,
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
    apply_furniture_texture_to_glb,
    convert_furniture_glb_to_usdz,
    convert_furniture_usdc_to_glb,
)
from server.core.config import settings
from shared.db import SessionLocal
from shared.models.furniture_model import FurnitureModel
from shared.models.room import Room
from shared.models.texture_preset import TexturePreset
from shared.models.user import User
from shared.models.version import Version

router = APIRouter()
bearer_scheme = HTTPBearer(auto_error=False)
USDC_CONTENT_TYPE = "application/octet-stream"
GLB_CONTENT_TYPE = "model/gltf-binary"


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


def _material_asset_model_key(model_key: str, material_preset_id: str) -> str:
    model_segment = _safe_s3_segment(model_key, "model")
    preset_segment = _safe_s3_segment(material_preset_id, "material")
    return f"{model_segment}_{preset_segment}"


def _version_asset_prefix(version: Version, material_model_key: str) -> str:
    version_json_key = _key_from_s3_uri(version.s3_json_url)
    if not version_json_key:
        raise HTTPException(status_code=400, detail="버전 JSON 경로가 없어 asset을 저장할 수 없습니다.")

    version_dir = version_json_key.rsplit("/", 1)[0]
    return f"{version_dir}/assets/{material_model_key}"


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


def _get_base_asset_model(db: Session, model_id: int) -> FurnitureModel:
    model = db.get(FurnitureModel, model_id)
    if model is None or model.status == "DELETED":
        raise HTTPException(status_code=404, detail="가구 모델을 찾을 수 없습니다.")
    if model.user_id is not None:
        raise HTTPException(status_code=400, detail="기본 가구 asset에만 텍스처를 적용할 수 있습니다.")
    return model


def _get_texture_preset(db: Session, preset_key: str) -> TexturePreset:
    preset = db.query(TexturePreset).filter(TexturePreset.preset_key == preset_key).one_or_none()
    if preset is None:
        raise HTTPException(status_code=404, detail="텍스처 preset을 찾을 수 없습니다.")
    return preset


def _resolve_material_version_context(
    db: Session,
    request: FurnitureMaterialAssetRegisterRequest | FurnitureMaterialAssetCompleteRequest,
    current_user: User,
) -> tuple[Room, Version]:
    room_id = request.room_id
    version = db.get(Version, request.version_id)
    if version is None:
        raise HTTPException(status_code=404, detail="버전을 찾을 수 없습니다.")
    if version.room_id != room_id:
        raise HTTPException(status_code=400, detail="room_id와 version_id가 일치하지 않습니다.")

    room = db.get(Room, room_id)
    if room is None:
        raise HTTPException(status_code=404, detail="방을 찾을 수 없습니다.")
    if room.user_id is not None and room.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="접근할 수 없는 방입니다.")

    return room, version


def _material_asset_key(version_id: int, furniture_instance_id: str, material_model_key: str) -> str:
    instance_segment = _safe_s3_segment(furniture_instance_id, "instance")
    return f"v{version_id}_{instance_segment}_{material_model_key}"


def _version_json_data(version: Version) -> dict:
    return dict(version.json_data) if isinstance(version.json_data, dict) else {}


def _get_material_asset_metadata(version: Version, furniture_instance_id: str) -> dict:
    json_data = _version_json_data(version)
    material_assets = json_data.get("material_assets") or {}
    if not isinstance(material_assets, dict):
        return {}
    asset = material_assets.get(furniture_instance_id) or {}
    return dict(asset) if isinstance(asset, dict) else {}


def _to_material_asset_response(
    model: FurnitureModel,
    version_id: int,
    room_id: int,
    furniture_instance_id: str,
    material_preset_id: str,
    material_name: str | None,
    status: str,
    glb_s3_key: str,
    usdz_s3_key: str,
    upload_url: str | None = None,
) -> FurnitureMaterialAssetResponse:
    material_model_key = _material_asset_model_key(model.model_key, material_preset_id)
    return FurnitureMaterialAssetResponse(
        material_asset_key=_material_asset_key(version_id, furniture_instance_id, material_model_key),
        status=status,
        base_model_id=model.id,
        model_key=model.model_key,
        material_model_key=material_model_key,
        room_id=room_id,
        version_id=version_id,
        furniture_instance_id=furniture_instance_id,
        material_preset_id=material_preset_id,
        material_name=material_name,
        glb_url=generate_presigned_url_for_uri(build_s3_uri(glb_s3_key)),
        usdz_url=generate_presigned_url_for_uri(build_s3_uri(usdz_s3_key)),
        upload_url=upload_url,
        upload_content_type=GLB_CONTENT_TYPE if upload_url else None,
        upload_s3_key=glb_s3_key if upload_url else None,
        glb_s3_key=glb_s3_key,
        usdz_s3_key=usdz_s3_key,
    )


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


def _sync_material_asset_version_metadata(
    version: Version,
    model: FurnitureModel,
    room_id: int,
    furniture_instance_id: str,
    material_preset_id: str,
    material_name: str | None,
    status: str,
    glb_s3_key: str,
    usdz_s3_key: str,
) -> None:
    json_data = _version_json_data(version)
    material_assets = dict(json_data.get("material_assets") or {})
    material_assets[furniture_instance_id] = {
        "status": status,
        "room_id": room_id,
        "version_id": version.id,
        "base_model_id": model.id,
        "model_key": model.model_key,
        "material_model_key": _material_asset_model_key(model.model_key, material_preset_id),
        "material_preset_id": material_preset_id,
        "material_name": material_name,
        "glb": build_s3_uri(glb_s3_key),
        "usdz": build_s3_uri(usdz_s3_key),
    }
    json_data["material_assets"] = material_assets
    version.json_data = json_data


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
    except (subprocess.SubprocessError, TimeoutError, ClientError, OSError, ValueError) as exc:
        model.status = "FAILED"
        db.commit()
        raise HTTPException(status_code=500, detail=f"가구 GLB 변환에 실패했습니다: {exc}") from exc

    model.status = "READY"
    model.glb_url = build_s3_uri(output_key)
    db.commit()
    db.refresh(model)
    return _to_model_response(model)


@router.post(
    "/models/{model_id}/material-assets/register",
    response_model=FurnitureMaterialAssetResponse,
    summary="기본 가구 텍스처 적용 asset 생성 준비",
)
async def register_furniture_material_asset(
    model_id: int,
    request: FurnitureMaterialAssetRegisterRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    model = _get_base_asset_model(db, model_id)
    preset = _get_texture_preset(db, request.material_preset_id)
    room, version = _resolve_material_version_context(db, request, current_user)
    material_name = request.material_name or preset.name

    material_model_key = _material_asset_model_key(model.model_key, request.material_preset_id)
    asset_prefix = _version_asset_prefix(version, material_model_key)
    glb_s3_key = f"{asset_prefix}.glb"
    usdz_s3_key = f"{asset_prefix}.usdz"

    _sync_material_asset_version_metadata(
        version,
        model,
        room_id=room.id,
        furniture_instance_id=request.furniture_instance_id,
        material_preset_id=request.material_preset_id,
        material_name=material_name,
        status="PENDING",
        glb_s3_key=glb_s3_key,
        usdz_s3_key=usdz_s3_key,
    )
    db.commit()

    return _to_material_asset_response(
        model,
        version_id=version.id,
        room_id=room.id,
        furniture_instance_id=request.furniture_instance_id,
        material_preset_id=request.material_preset_id,
        material_name=material_name,
        status="PENDING",
        glb_s3_key=glb_s3_key,
        usdz_s3_key=usdz_s3_key,
    )


@router.post(
    "/models/{model_id}/material-assets/complete",
    response_model=FurnitureMaterialAssetResponse,
    summary="기본 가구 텍스처 적용 GLB/USDZ 생성",
)
async def complete_furniture_material_asset_upload(
    model_id: int,
    request: FurnitureMaterialAssetCompleteRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    model = _get_base_asset_model(db, model_id)
    preset = _get_texture_preset(db, request.material_preset_id)
    room, version = _resolve_material_version_context(db, request, current_user)

    material_model_key = _material_asset_model_key(model.model_key, request.material_preset_id)
    asset_prefix = _version_asset_prefix(version, material_model_key)
    glb_s3_key = f"{asset_prefix}.glb"
    usdz_s3_key = f"{asset_prefix}.usdz"
    existing = _get_material_asset_metadata(version, request.furniture_instance_id)
    material_name = existing.get("material_name") if isinstance(existing.get("material_name"), str) else preset.name
    base_glb_s3_key = _key_from_s3_uri(model.glb_url)

    if not base_glb_s3_key:
        raise HTTPException(status_code=400, detail="기본 가구 GLB 경로가 없습니다.")
    if not await object_exists(base_glb_s3_key):
        raise HTTPException(status_code=400, detail="기본 가구 GLB 파일을 찾을 수 없습니다.")
    if not await object_exists(preset.texture_s3_key):
        raise HTTPException(status_code=400, detail="텍스처 preset 파일을 찾을 수 없습니다.")

    _sync_material_asset_version_metadata(
        version,
        model,
        room_id=room.id,
        furniture_instance_id=request.furniture_instance_id,
        material_preset_id=request.material_preset_id,
        material_name=material_name,
        status="PROCESSING",
        glb_s3_key=glb_s3_key,
        usdz_s3_key=usdz_s3_key,
    )
    db.commit()

    try:
        await apply_furniture_texture_to_glb(base_glb_s3_key, preset.texture_s3_key, glb_s3_key)
        await convert_furniture_glb_to_usdz(glb_s3_key, usdz_s3_key)
    except (subprocess.SubprocessError, TimeoutError, ClientError, OSError, ValueError, RuntimeError) as exc:
        _sync_material_asset_version_metadata(
            version,
            model,
            room_id=room.id,
            furniture_instance_id=request.furniture_instance_id,
            material_preset_id=request.material_preset_id,
            material_name=material_name,
            status="FAILED",
            glb_s3_key=glb_s3_key,
            usdz_s3_key=usdz_s3_key,
        )
        db.commit()
        raise HTTPException(status_code=500, detail=f"텍스처 적용 가구 USDZ 변환에 실패했습니다: {exc}") from exc

    _sync_material_asset_version_metadata(
        version,
        model,
        room_id=room.id,
        furniture_instance_id=request.furniture_instance_id,
        material_preset_id=request.material_preset_id,
        material_name=material_name,
        status="READY",
        glb_s3_key=glb_s3_key,
        usdz_s3_key=usdz_s3_key,
    )
    db.commit()

    return _to_material_asset_response(
        model,
        version_id=version.id,
        room_id=room.id,
        furniture_instance_id=request.furniture_instance_id,
        material_preset_id=request.material_preset_id,
        material_name=material_name,
        status="READY",
        glb_s3_key=glb_s3_key,
        usdz_s3_key=usdz_s3_key,
    )


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
