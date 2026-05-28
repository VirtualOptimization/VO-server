import asyncio
import json
import logging
import uuid
from typing import Any

import boto3
from fastapi import APIRouter, File, HTTPException, UploadFile
from pydantic import BaseModel

from server.core.config import settings
from server.core.s3 import (
    build_s3_uri,
    generate_presigned_put_url,
    object_exists,
    upload_json,
)
from server.schemas.scan import (
    PresignedUploadTarget,
    ScanUploadCompleteRequest,
    ScanUploadCompleteResponse,
    ScanUploadStartRequest,
    ScanUploadStartResponse,
)
from shared.db import SessionLocal
from shared.models.furniture_item import FurnitureItem
from shared.models.room import Room
from shared.models.version import Version

logger = logging.getLogger(__name__)
router = APIRouter()

RAW_ROOT_PREFIX = "scans"
MODEL_CONTENT_TYPE = "application/octet-stream"
USDZ_CONTENT_TYPE = "model/vnd.usdz+zip"
JSON_CONTENT_TYPE = "application/json"
URL_EXPIRATION_SECONDS = 3600

stepfunctions_client = boto3.client(
    "stepfunctions",
    region_name=settings.aws_region,
    aws_access_key_id=settings.aws_access_key_id,
    aws_secret_access_key=settings.aws_secret_access_key,
)


def _raw_prefix(confirm_code: str) -> str:
    return f"{RAW_ROOT_PREFIX}/{confirm_code}/origin"


def _generated_prefix(confirm_code: str) -> str:
    return f"{RAW_ROOT_PREFIX}/{confirm_code}/optimized"


def _generate_unique_confirm_code(db) -> str:
    while True:
        confirm_code = uuid.uuid4().hex[:6].upper()
        existing = db.query(Room.id).filter(Room.confirm_code == confirm_code).first()
        if existing is None:
            return confirm_code


def _create_upload_session(
    include_room_usdz: bool,
    include_room_empty_usdz: bool,
) -> tuple[int, str]:
    db = SessionLocal()
    try:
        confirm_code = _generate_unique_confirm_code(db)
        room_shell_key = f"{_raw_prefix(confirm_code)}/Room.usdz" if include_room_usdz else None
        
        room = Room(
            confirm_code=confirm_code,
            status="PENDING",
            room_shell_usdc_url=build_s3_uri(room_shell_key) if room_shell_key else None,
        )
        db.add(room)
        db.commit()
        db.refresh(room)
        return room.id, confirm_code
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def _mark_upload_completed(confirm_code: str, uploaded_keys: list[str], pipeline_started: bool) -> int:
    db = SessionLocal()
    try:
        room = db.query(Room).filter(Room.confirm_code == confirm_code).first()
        if not room:
            raise ValueError(f"Room with confirm_code {confirm_code} not found")
        
        if pipeline_started:
            room.status = "PROCESSING"
        else:
            room.status = "UPLOADED"
            
        # Room.usdz가 업로드된 경우 URL 업데이트
        room_shell_key = next((k for k in uploaded_keys if k.endswith("/Room.usdz")), None)
        if room_shell_key:
            room.room_shell_usdc_url = build_s3_uri(room_shell_key)
            
        db.commit()
        return room.id
    finally:
        db.close()


def _build_pipeline_input(room_id: int, confirm_code: str, uploaded_keys: list[str]) -> dict[str, Any]:
    raw_prefix = _raw_prefix(confirm_code)
    generated_prefix = _generated_prefix(confirm_code)

    room_usdz_key = next((k for k in uploaded_keys if k.endswith("/Room.usdz")), None)
    room_empty_usdz_key = next((k for k in uploaded_keys if k.endswith("/Room_empty.usdz")), None)
    model_keys = sorted(k for k in uploaded_keys if "/Models/" in k)

    return {
        "room_id": room_id,
        "confirm_code": confirm_code,
        "source": "ios_upload",
        "pipeline_version": "v1",
        "raw_prefix": raw_prefix,
        "generated_prefix": generated_prefix,
        "inputs": {
            "room_data_json": f"{raw_prefix}/room_data.json",
            "room_usdz": room_usdz_key,
            "room_empty_usdz": room_empty_usdz_key,
            "models": model_keys,
        },
        "outputs": {
            "normalized_json": f"{generated_prefix}/room_data.normalized.json",
            "problem_json": f"{generated_prefix}/room_data.problem.json",
            "optimized_json": f"{generated_prefix}/room_data.optimized.json",
            "roomplan_optimized_json": f"{generated_prefix}/room_data.roomplan_optimized.json",
            "fbx": f"{generated_prefix}/output.fbx",
        },
    }


async def _start_pipeline_execution(pipeline_input: dict[str, Any]) -> str | None:
    try:
        confirm_code = pipeline_input["confirm_code"]
        response = stepfunctions_client.start_execution(
            stateMachineArn=settings.step_functions_state_machine_arn,
            name=f"VO-Scan-{confirm_code}-{uuid.uuid4().hex[:8].upper()}",
            input=json.dumps(pipeline_input)
        )
        return response["executionArn"]
    except Exception as e:
        logger.error(f"Step Functions start_execution failed: {str(e)}")
        return None


def _save_optimized_to_db(room_id: int, optimized_data: dict[str, Any], optimized_s3_url: str | None = None) -> None:
    db = SessionLocal()
    try:
        # UPSERT logic for OPTIMIZED version
        existing_version = db.query(Version).filter(
            Version.room_id == room_id,
            Version.version_type == "OPTIMIZED",
            Version.version_no == 1
        ).first()

        if existing_version:
            logger.info(f"Existing OPTIMIZED v1 found for room_id {room_id}. Updating...")
            db.query(FurnitureItem).filter(FurnitureItem.version_id == existing_version.id).delete()
            version = existing_version
            version.json_data = optimized_data
            if optimized_s3_url:
                version.s3_json_url = optimized_s3_url
        else:
            logger.info(f"Creating new OPTIMIZED v1 for room_id {room_id}.")
            version = Version(
                room_id=room_id,
                version_no=1,
                version_type="OPTIMIZED",
                json_data=optimized_data,
                s3_json_url=optimized_s3_url
            )
            db.add(version)
            db.flush() 

        elements = optimized_data.get("elements", [])
        for elem in elements:
            transform_dict = elem.get("transform_dict", {})
            
            furniture = FurnitureItem(
                version_id=version.id,
                model_id=elem.get("model_id"),
                item_key=elem.get("item_key", f"item_{uuid.uuid4().hex[:8]}"),
                
                pos_x=transform_dict.get("pos_x", 0.0),
                pos_y=transform_dict.get("pos_y", 0.0),
                pos_z=transform_dict.get("pos_z", 0.0),
                rot_x=transform_dict.get("rot_x", 0.0),
                rot_y=transform_dict.get("rot_y", 0.0),
                rot_z=transform_dict.get("rot_z", 0.0),
                scale_x=transform_dict.get("scale_x", 1.0),
                scale_y=transform_dict.get("scale_y", 1.0),
                scale_z=transform_dict.get("scale_z", 1.0),
                
                footprint_polygon=elem.get("footprint_polygon")
            )
            db.add(furniture)

        db.commit()
    except Exception as e:
        db.rollback()
        logger.error(f"Failed to persist optimized layout to database: {str(e)}")
        raise
    finally:
        db.close()


@router.post("/start", response_model=ScanUploadStartResponse)
async def start_scan_upload(request: ScanUploadStartRequest):
    room_id, confirm_code = _create_upload_session(
        include_room_usdz=request.include_room_usdz,
        include_room_empty_usdz=request.include_room_empty_usdz,
    )
    raw_prefix = _raw_prefix(confirm_code)
    targets = []

    # 1. room_data.json 업로드 URL 추가 (필수)
    s3_json_key = f"{raw_prefix}/room_data.json"
    targets.append(PresignedUploadTarget(
        logical_name="room_data_json",
        s3_key=s3_json_key,
        presigned_url=await generate_presigned_put_url(s3_json_key, JSON_CONTENT_TYPE),
        content_type=JSON_CONTENT_TYPE
    ))

    # 2. USDZ 룸 파일 처리
    if request.include_room_usdz:
        s3_key = f"{raw_prefix}/Room.usdz"
        targets.append(PresignedUploadTarget(
            logical_name="room_usdz",
            s3_key=s3_key,
            presigned_url=await generate_presigned_put_url(s3_key, USDZ_CONTENT_TYPE),
            content_type=USDZ_CONTENT_TYPE
        ))

    # 3. 비어있는 룸 파일 처리
    if request.include_room_empty_usdz:
        s3_key = f"{raw_prefix}/Room_empty.usdz"
        targets.append(PresignedUploadTarget(
            logical_name="room_empty_usdz",
            s3_key=s3_key,
            presigned_url=await generate_presigned_put_url(s3_key, USDZ_CONTENT_TYPE),
            content_type=USDZ_CONTENT_TYPE
        ))

    # 4. 개별 가구 모델 파일들 처리
    for filename in request.model_filenames:
        s3_key = f"{raw_prefix}/Models/{filename}"
        targets.append(PresignedUploadTarget(
            logical_name=f"model_{filename}",
            s3_key=s3_key,
            presigned_url=await generate_presigned_put_url(s3_key, MODEL_CONTENT_TYPE),
            content_type=MODEL_CONTENT_TYPE
        ))

    return ScanUploadStartResponse(
        message="Upload session successfully initialized.",
        room_id=room_id,
        confirm_code=confirm_code,
        raw_prefix=raw_prefix,
        generated_prefix=_generated_prefix(confirm_code),
        expires_in_seconds=URL_EXPIRATION_SECONDS,
        uploads=targets
    )


@router.post("/{confirm_code}/complete", response_model=ScanUploadCompleteResponse)
async def complete_scan_upload(confirm_code: str, payload: ScanUploadCompleteRequest):
    if not payload.uploaded_keys:
        raise HTTPException(status_code=422, detail="uploaded_keys는 비어 있을 수 없습니다.")

    raw_prefix = _raw_prefix(confirm_code)
    required_json_key = f"{raw_prefix}/room_data.json"

    # Prefix 검증
    invalid_keys = [k for k in payload.uploaded_keys if not k.startswith(f"{raw_prefix}/")]
    if invalid_keys:
        raise HTTPException(
            status_code=422, 
            detail={"message": "uploaded_keys 중 raw prefix 외 경로가 있습니다.", "invalid_keys": invalid_keys}
        )

    # 필수 파일 검증
    if required_json_key not in payload.uploaded_keys:
        raise HTTPException(status_code=422, detail=f"필수 파일 누락: {required_json_key}")

    # S3 실재 여부 검증
    missing_keys = []
    for k in payload.uploaded_keys:
        if not await object_exists(k):
            missing_keys.append(k)
    if missing_keys:
        raise HTTPException(
            status_code=409, 
            detail={"message": "아직 업로드되지 않은 파일이 있습니다.", "missing_keys": missing_keys}
        )

    # 상태 업데이트 및 room_id 획득
    room_id = await asyncio.to_thread(_mark_upload_completed, confirm_code, payload.uploaded_keys, False)

    # 파이프라인 트리거
    pipeline_input = _build_pipeline_input(room_id, confirm_code, payload.uploaded_keys)
    execution_arn = await _start_pipeline_execution(pipeline_input)
    pipeline_started = execution_arn is not None

    if pipeline_started:
        # 트리거 성공 시 상태를 PROCESSING으로 변경
        await asyncio.to_thread(_mark_upload_completed, confirm_code, payload.uploaded_keys, True)

    return ScanUploadCompleteResponse(
        message="scan upload completed",
        room_id=room_id,
        confirm_code=confirm_code,
        raw_prefix=raw_prefix,
        generated_prefix=_generated_prefix(confirm_code),
        uploaded_keys=payload.uploaded_keys,
        pipeline_started=pipeline_started,
        execution_arn=execution_arn,
        pipeline_input=pipeline_input,
    )


@router.post("/{confirm_code}/optimize")
async def save_optimized_layout_endpoint(confirm_code: str, optimized_result: dict[str, Any], optimized_s3_url: str | None = None):
    db = SessionLocal()
    try:
        room = db.query(Room).filter(Room.confirm_code == confirm_code).first()
        if not room:
            raise HTTPException(status_code=404, detail="Room not found")
        
        await asyncio.to_thread(_save_optimized_to_db, room.id, optimized_result, optimized_s3_url)
        return {"message": "Optimized layout saved"}
    except Exception as e:
        logger.error(f"Failed to save optimized layout: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        db.close()


@router.post("", include_in_schema=False)
async def legacy_upload_zip(file: UploadFile = File(...)):
    return {"message": "Legacy zip upload is deprecated. Use /start and /complete instead."}


@router.post("/{confirm_code}/trigger", include_in_schema=False)
async def legacy_trigger_pipeline(confirm_code: str):
    return {"message": "Legacy trigger is deprecated. Use /complete instead."}
