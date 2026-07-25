"""공간 스캔 — 업로드 세션 시작 / 완료"""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from server.api.v1.deps import get_current_user
from server.api.v1.endpoints.rooms_common import (
    JSON_CONTENT_TYPE,
    MODEL_CONTENT_TYPE,
    USDZ_CONTENT_TYPE,
    URL_EXPIRATION_SECONDS,
    _generated_prefix,
    _raw_prefix,
    _user_s3_segment,
)
from server.core.config import settings
from server.core.s3 import build_s3_uri, generate_presigned_put_url, get_json, object_exists, upload_json
from server.services.local_scan_pipeline import run_local_scan_pipeline
from server.schemas.scan import (
    PresignedUploadTarget,
    ScanUploadCompleteRequest,
    ScanUploadCompleteResponse,
    ScanUploadStartRequest,
    ScanUploadStartResponse,
)
from shared.db import SessionLocal
from shared.models.room import Room
from shared.models.user import User

logger = logging.getLogger(__name__)
router = APIRouter()


# ── 내부 헬퍼 ────────────────────────────────────────────────────────────────

def _get_stepfunctions_client():
    import boto3

    client_kwargs = {"region_name": settings.aws_region}
    if settings.aws_access_key_id and settings.aws_secret_access_key:
        client_kwargs.update(
            aws_access_key_id=settings.aws_access_key_id,
            aws_secret_access_key=settings.aws_secret_access_key,
        )
    return boto3.client("stepfunctions", **client_kwargs)

def _generate_unique_confirm_code(db) -> str:
    while True:
        code = uuid.uuid4().hex[:6].upper()
        if db.query(Room.id).filter(Room.confirm_code == code).first() is None:
            return code


def _create_upload_session(
    include_room_usdz: bool,
    include_room_empty_usdz: bool,
    user_id: int | None = None,
    owner_segment: str | None = None,
) -> int:
    db = SessionLocal()
    try:
        confirm_code = _generate_unique_confirm_code(db)
        room = Room(
            user_id=user_id,
            confirm_code=confirm_code,
            status="PENDING",
        )
        db.add(room)
        db.flush()
        raw_prefix = _raw_prefix(str(room.id), owner_segment)
        room_shell_key = f"{raw_prefix}/Room.usdz" if include_room_usdz else None
        room.room_shell_usdc_url = build_s3_uri(room_shell_key) if room_shell_key else None
        db.commit()
        db.refresh(room)
        return room.id
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def _mark_upload_completed(room_id: int, uploaded_keys: list[str], pipeline_started: bool) -> int:
    db = SessionLocal()
    try:
        room = db.get(Room, room_id)
        if not room:
            raise ValueError(f"Room not found: {room_id}")
        room.status = "PROCESSING" if pipeline_started else "UPLOADED"
        room_shell_key = next((k for k in uploaded_keys if k.endswith("/Room.usdz")), None)
        if room_shell_key:
            room.room_shell_usdc_url = build_s3_uri(room_shell_key)
        db.commit()
        return room.id
    finally:
        db.close()


def _get_room_owner_segment(room_id: int, user_id: int) -> str | None:
    db = SessionLocal()
    try:
        room = db.query(Room).filter(Room.id == room_id, Room.user_id == user_id).first()
        if room is None:
            raise ValueError(f"Room not found: {room_id}")
        user = db.get(User, room.user_id)
        return _user_s3_segment(user)
    finally:
        db.close()


def _build_pipeline_input(
    room_id: int,
    uploaded_keys: list[str],
    owner_segment: str | None = None,
) -> dict[str, Any]:
    room_ref = str(room_id)
    raw_prefix = _raw_prefix(room_ref, owner_segment)
    generated_prefix = _generated_prefix(room_ref, owner_segment)
    return {
        "bucket": settings.s3_bucket_name,
        "room_id": room_id,
        "source": "ios_upload",
        "pipeline_version": "v1",
        "raw_prefix": raw_prefix,
        "generated_prefix": generated_prefix,
        "inputs": {
            "room_data_json": f"{raw_prefix}/room_data.json",
            "unity_room_data_json": f"{raw_prefix}/room_data.unity.json",
            "room_usdz": next((k for k in uploaded_keys if k.endswith("/Room.usdz")), None),
            "room_empty_usdz": next((k for k in uploaded_keys if k.endswith("/Room_empty.usdz")), None),
            "models": sorted(k for k in uploaded_keys if "/models/" in k),
        },
        "outputs": {
            "normalized_json": f"{generated_prefix}/room_data.normalized.json",
            "problem_json": f"{generated_prefix}/room_data.problem.json",
            "optimized_json": f"{generated_prefix}/room_data.optimized.json",
            "roomplan_optimized_json": f"{generated_prefix}/room_data.roomplan_optimized.json",
            "unity_roomplan_optimized_json": f"{generated_prefix}/room_data.roomplan_optimized.unity.json",
            "glb": f"{raw_prefix}/output.glb",
        },
    }


async def _create_origin_unity_json(raw_prefix: str) -> str:
    from server.services.transform.unity_roomplan import normalize_roomplan_for_unity

    room_data_key = f"{raw_prefix}/room_data.json"
    unity_room_data_key = f"{raw_prefix}/room_data.unity.json"
    room_data = await get_json(room_data_key)
    unity_room_data = normalize_roomplan_for_unity(room_data)
    await upload_json(unity_room_data_key, unity_room_data)
    return unity_room_data_key


async def _start_pipeline_execution(pipeline_input: dict[str, Any]) -> str | None:
    try:
        room_id = pipeline_input["room_id"]
        response = _get_stepfunctions_client().start_execution(
            stateMachineArn=settings.step_functions_state_machine_arn,
            name=f"VO-Scan-{room_id}-{uuid.uuid4().hex[:8].upper()}",
            input=json.dumps(pipeline_input),
        )
        return response["executionArn"]
    except Exception as e:
        logger.error(f"Step Functions start_execution failed: {e}")
        return None


# ── POST /rooms/start ─────────────────────────────────────────────────────────

@router.post("/start", response_model=ScanUploadStartResponse)
async def start_scan_upload(
    request: ScanUploadStartRequest,
    current_user: User = Depends(get_current_user),
):
    owner_segment = _user_s3_segment(current_user)
    room_id = _create_upload_session(
        include_room_usdz=request.include_room_usdz,
        include_room_empty_usdz=request.include_room_empty_usdz,
        user_id=current_user.id,
        owner_segment=owner_segment,
    )
    raw_prefix = _raw_prefix(str(room_id), owner_segment)
    generated_prefix = _generated_prefix(str(room_id), owner_segment)
    targets = []

    s3_json_key = f"{raw_prefix}/room_data.json"
    targets.append(PresignedUploadTarget(
        logical_name="room_data_json", s3_key=s3_json_key,
        presigned_url=await generate_presigned_put_url(s3_json_key, JSON_CONTENT_TYPE),
        content_type=JSON_CONTENT_TYPE,
    ))
    if request.include_room_usdz:
        s3_key = f"{raw_prefix}/Room.usdz"
        targets.append(PresignedUploadTarget(
            logical_name="room_usdz", s3_key=s3_key,
            presigned_url=await generate_presigned_put_url(s3_key, USDZ_CONTENT_TYPE),
            content_type=USDZ_CONTENT_TYPE,
        ))
    if request.include_room_empty_usdz:
        s3_key = f"{raw_prefix}/Room_empty.usdz"
        targets.append(PresignedUploadTarget(
            logical_name="room_empty_usdz", s3_key=s3_key,
            presigned_url=await generate_presigned_put_url(s3_key, USDZ_CONTENT_TYPE),
            content_type=USDZ_CONTENT_TYPE,
        ))

    return ScanUploadStartResponse(
        message="Upload session successfully initialized.",
        room_id=room_id,
        raw_prefix=raw_prefix, generated_prefix=generated_prefix,
        expires_in_seconds=URL_EXPIRATION_SECONDS, uploads=targets,
    )


# ── POST /rooms/{room_id}/complete ───────────────────────────────────────────

@router.post("/{room_id}/complete", response_model=ScanUploadCompleteResponse)
async def complete_scan_upload(
    room_id: int,
    payload: ScanUploadCompleteRequest,
    current_user: User = Depends(get_current_user),
):
    if not payload.uploaded_keys:
        raise HTTPException(status_code=422, detail="uploaded_keys는 비어 있을 수 없습니다.")

    try:
        owner_segment = _get_room_owner_segment(room_id, current_user.id)
    except ValueError:
        raise HTTPException(status_code=404, detail="방을 찾을 수 없습니다.")
    raw_prefix = _raw_prefix(str(room_id), owner_segment)
    generated_prefix = _generated_prefix(str(room_id), owner_segment)
    required_json_key = f"{raw_prefix}/room_data.json"

    invalid_keys = [k for k in payload.uploaded_keys if not k.startswith(f"{raw_prefix}/")]
    if invalid_keys:
        raise HTTPException(status_code=422, detail={"message": "잘못된 경로가 포함됐습니다.", "invalid_keys": invalid_keys})

    if required_json_key not in payload.uploaded_keys:
        raise HTTPException(status_code=422, detail=f"필수 파일 누락: {required_json_key}")

    missing_keys = [k for k in payload.uploaded_keys if not await object_exists(k)]
    if missing_keys:
        raise HTTPException(status_code=409, detail={"message": "아직 업로드되지 않은 파일이 있습니다.", "missing_keys": missing_keys})

    unity_room_data_key = await _create_origin_unity_json(raw_prefix)

    # 이미 파이프라인이 실행 중이면 중복 실행 방지
    db_check = SessionLocal()
    try:
        room_check = db_check.get(Room, room_id)
        if room_check and room_check.status in ("PROCESSING", "COMPLETED"):
            return ScanUploadCompleteResponse(
                message="already processing", room_id=room_check.id,
                raw_prefix=raw_prefix, generated_prefix=generated_prefix,
                uploaded_keys=payload.uploaded_keys, pipeline_started=True,
                execution_arn=None, pipeline_input={},
            )
    finally:
        db_check.close()

    room_id = await asyncio.to_thread(_mark_upload_completed, room_id, payload.uploaded_keys, False)
    processed_keys = [*payload.uploaded_keys, unity_room_data_key]
    pipeline_input = _build_pipeline_input(room_id, processed_keys, owner_segment)

    is_local_pipeline = settings.scan_pipeline_mode.lower() == "local"

    if is_local_pipeline:
        await run_local_scan_pipeline(pipeline_input)
        execution_arn = None
        pipeline_started = True
    else:
        execution_arn = await _start_pipeline_execution(pipeline_input)
        pipeline_started = execution_arn is not None

    if pipeline_started and not is_local_pipeline:
        await asyncio.to_thread(_mark_upload_completed, room_id, payload.uploaded_keys, True)

    return ScanUploadCompleteResponse(
        message="scan upload completed", room_id=room_id,
        raw_prefix=raw_prefix, generated_prefix=generated_prefix,
        uploaded_keys=processed_keys, pipeline_started=pipeline_started,
        execution_arn=execution_arn, pipeline_input=pipeline_input,
    )
