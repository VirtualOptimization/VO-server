import asyncio
import io
import json
import logging
import uuid
import zipfile
from typing import Any

import boto3
from fastapi import APIRouter, File, HTTPException, UploadFile

from server.core.config import settings
from server.core.s3 import (
    build_s3_uri,
    generate_presigned_put_url,
    get_json,
    object_exists,
    upload_bytes,
    upload_json,
)
from server.schemas.scan import (
    IOSRoomScanData,
    PresignedUploadTarget,
    ScanUploadCompleteRequest,
    ScanUploadCompleteResponse,
    ScanUploadResponse,
    ScanUploadStartRequest,
    ScanUploadStartResponse,
)
from server.services.transform.problem import build_layout_problem
from server.services.transform.roomplan import convert_roomplan_to_optimizer_payload
from server.services.transform.roomplan_export import export_optimized_layout_to_roomplan
from shared.db import SessionLocal
from shared.models.furniture_item import FurnitureItem
from shared.models.room import Room
from shared.models.version import Version
from workers.optimizer.layout import CanonicalLayoutOptimizer

logger = logging.getLogger(__name__)
router = APIRouter()

RAW_ROOT_PREFIX = "scans"
MODEL_CONTENT_TYPE = "application/octet-stream"
USDZ_CONTENT_TYPE = "model/vnd.usdz+zip"

stepfunctions_client = boto3.client(
    "stepfunctions",
    region_name=settings.aws_region,
    aws_access_key_id=settings.aws_access_key_id,
    aws_secret_access_key=settings.aws_secret_access_key,
)


def _raw_prefix(confirm_code: str) -> str:
    return f"{RAW_ROOT_PREFIX}/{confirm_code}/raw"


def _generated_prefix(confirm_code: str) -> str:
    return f"{RAW_ROOT_PREFIX}/{confirm_code}/generated"


def _build_generated_outputs(confirm_code: str) -> dict[str, str]:
    generated_prefix = _generated_prefix(confirm_code)
    return {
        "normalized_json": f"{generated_prefix}/room_data.normalized.json",
        "problem_json": f"{generated_prefix}/room_data.problem.json",
        "optimized_json": f"{generated_prefix}/room_data.optimized.json",
        "roomplan_optimized_json": f"{generated_prefix}/room_data.roomplan_optimized.json",
        "fbx": f"{generated_prefix}/output.fbx",
    }


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
            status="UPLOADING",
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


def _build_upload_targets(
    confirm_code: str,
    request: ScanUploadStartRequest,
) -> list[tuple[str, str, str]]:
    raw_prefix = _raw_prefix(confirm_code)
    targets: list[tuple[str, str, str]] = [
        ("room_data_json", f"{raw_prefix}/room_data.json", "application/json"),
    ]
    if request.include_room_usdz:
        targets.append(("room_usdz", f"{raw_prefix}/Room.usdz", USDZ_CONTENT_TYPE))
    if request.include_room_empty_usdz:
        targets.append(("room_empty_usdz", f"{raw_prefix}/Room_empty.usdz", USDZ_CONTENT_TYPE))
    for filename in request.model_filenames:
        safe_name = filename.split("/")[-1]
        targets.append((f"model:{safe_name}", f"{raw_prefix}/Models/{safe_name}", MODEL_CONTENT_TYPE))
    return targets


def _mark_upload_completed(
    confirm_code: str,
    uploaded_keys: list[str],
    pipeline_started: bool,
) -> int:
    db = SessionLocal()
    try:
        room = db.query(Room).filter(Room.confirm_code == confirm_code).first()
        if room is None:
            raise ValueError(f"Room not found: {confirm_code}")
        room.status = "PROCESSING" if pipeline_started else "UPLOADED"
        room_usdz_key = next((key for key in uploaded_keys if key.endswith("/Room.usdz")), None)
        if room_usdz_key:
            room.room_shell_usdc_url = build_s3_uri(room_usdz_key)
        db.commit()
        return room.id
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


async def _start_pipeline_execution(pipeline_input: dict[str, Any]) -> str | None:
    if not settings.step_functions_state_machine_arn:
        return None

    response = await asyncio.to_thread(
        stepfunctions_client.start_execution,
        stateMachineArn=settings.step_functions_state_machine_arn,
        name=f"scan-{pipeline_input['confirm_code']}-{uuid.uuid4().hex[:8]}",
        input=json.dumps(pipeline_input),
    )
    return response["executionArn"]


def _build_pipeline_input(
    room_id: int,
    confirm_code: str,
    uploaded_keys: list[str],
) -> dict[str, Any]:
    raw_prefix = _raw_prefix(confirm_code)
    generated_prefix = _generated_prefix(confirm_code)

    room_usdz_key = next((key for key in uploaded_keys if key.endswith("/Room.usdz")), None)
    room_empty_usdz_key = next((key for key in uploaded_keys if key.endswith("/Room_empty.usdz")), None)
    model_keys = sorted(key for key in uploaded_keys if "/Models/" in key)

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
        "outputs": _build_generated_outputs(confirm_code),
    }


def _save_original_to_db(confirm_code: str, s3_url: str, layout_problem: dict[str, Any]) -> None:
    db = SessionLocal()
    try:
        room = Room(confirm_code=confirm_code, status="PENDING")
        db.add(room)
        db.flush()

        version = Version(
            room_id=room.id,
            version_type="ORIGINAL",
            version_no=0,
            s3_json_url=s3_url,
            json_data=layout_problem,
        )
        db.add(version)
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def _save_optimized_to_db(
    confirm_code: str,
    optimized_s3_url: str,
    optimized_result: dict[str, Any],
) -> None:
    db = SessionLocal()
    try:
        room = db.query(Room).filter(Room.confirm_code == confirm_code).first()
        if not room:
            raise ValueError(f"Room not found: {confirm_code}")

        original_version = (
            db.query(Version)
            .filter(Version.room_id == room.id, Version.version_type == "ORIGINAL")
            .first()
        )

        optimized_version = Version(
            room_id=room.id,
            version_type="OPTIMIZED",
            version_no=1,
            parent_version_id=original_version.id if original_version else None,
            s3_json_url=optimized_s3_url,
            json_data=optimized_result,
        )
        db.add(optimized_version)
        db.flush()

        for item in optimized_result.get("movable_items", []):
            pos = item.get("optimized_pos") or item["pos"]
            rot_y = float(item.get("optimized_rotation_y_deg", item.get("rotation_y_deg", 0.0)))
            db.add(FurnitureItem(
                version_id=optimized_version.id,
                item_key=item["id"],
                pos_x=float(pos[0]),
                pos_y=float(pos[1]),
                pos_z=float(pos[2]),
                rot_x=0.0,
                rot_y=rot_y,
                rot_z=0.0,
                footprint_polygon=item.get("footprint_polygon"),
            ))

        room.status = "COMPLETED"
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


@router.post("/start", response_model=ScanUploadStartResponse)
async def start_scan_upload(payload: ScanUploadStartRequest):
    room_id, confirm_code = await asyncio.to_thread(
        _create_upload_session,
        payload.include_room_usdz,
        payload.include_room_empty_usdz,
    )
    raw_prefix = _raw_prefix(confirm_code)
    generated_prefix = _generated_prefix(confirm_code)

    uploads: list[PresignedUploadTarget] = []
    for logical_name, s3_key, content_type in _build_upload_targets(confirm_code, payload):
        presigned_url = await generate_presigned_put_url(s3_key, content_type)
        uploads.append(
            PresignedUploadTarget(
                logical_name=logical_name,
                s3_key=s3_key,
                presigned_url=presigned_url,
                content_type=content_type,
            )
        )

    return ScanUploadStartResponse(
        message="scan upload session created",
        room_id=room_id,
        confirm_code=confirm_code,
        raw_prefix=raw_prefix,
        generated_prefix=generated_prefix,
        expires_in_seconds=settings.s3_presigned_expiration_seconds,
        uploads=uploads,
    )


@router.post("/{confirm_code}/complete", response_model=ScanUploadCompleteResponse)
async def complete_scan_upload(confirm_code: str, payload: ScanUploadCompleteRequest):
    if not payload.uploaded_keys:
        raise HTTPException(status_code=422, detail="uploaded_keys는 비어 있을 수 없습니다.")

    raw_prefix = _raw_prefix(confirm_code)
    generated_prefix = _generated_prefix(confirm_code)

    invalid_keys = [key for key in payload.uploaded_keys if not key.startswith(f"{raw_prefix}/")]
    if invalid_keys:
        raise HTTPException(
            status_code=422,
            detail={
                "message": "uploaded_keys 중 raw prefix에 속하지 않는 항목이 있습니다.",
                "invalid_keys": invalid_keys,
            },
        )

    required_json_key = f"{raw_prefix}/room_data.json"
    if required_json_key not in payload.uploaded_keys:
        raise HTTPException(
            status_code=422,
            detail=f"필수 파일이 없습니다: {required_json_key}",
        )

    missing_keys: list[str] = []
    for key in payload.uploaded_keys:
        if not await object_exists(key):
            missing_keys.append(key)

    if missing_keys:
        raise HTTPException(
            status_code=409,
            detail={
                "message": "아직 업로드되지 않은 파일이 있습니다.",
                "missing_keys": missing_keys,
            },
        )

    room_id = await asyncio.to_thread(
        _mark_upload_completed,
        confirm_code,
        payload.uploaded_keys,
        False,
    )
    pipeline_input = _build_pipeline_input(room_id, confirm_code, payload.uploaded_keys)
    execution_arn = await _start_pipeline_execution(pipeline_input)
    pipeline_started = execution_arn is not None
    if pipeline_started:
        await asyncio.to_thread(
            _mark_upload_completed,
            confirm_code,
            payload.uploaded_keys,
            True,
        )

    return ScanUploadCompleteResponse(
        message="scan upload completed",
        room_id=room_id,
        confirm_code=confirm_code,
        raw_prefix=raw_prefix,
        generated_prefix=generated_prefix,
        uploaded_keys=payload.uploaded_keys,
        pipeline_started=pipeline_started,
        execution_arn=execution_arn,
        pipeline_input=pipeline_input,
    )


# ── POST /scans ───────────────────────────────────────────────────────────────
# 스캔완료 버튼: zip 업로드 → S3 저장 → 확인코드 반환

@router.post("", response_model=ScanUploadResponse)
async def upload_scan(
    scan_export: UploadFile = File(..., description="ScanExport.zip"),
):
    raw_zip = await scan_export.read()
    try:
        zf = zipfile.ZipFile(io.BytesIO(raw_zip))
    except zipfile.BadZipFile:
        raise HTTPException(status_code=422, detail="유효한 zip 파일이 아닙니다.")

    names = zf.namelist()

    json_name = next((n for n in names if n.endswith("room_data.json")), None)
    if not json_name:
        raise HTTPException(status_code=422, detail="zip 안에 room_data.json이 없습니다.")
    try:
        raw_json = json.loads(zf.read(json_name))
        scan_data = IOSRoomScanData(**raw_json)
    except Exception as e:
        raise HTTPException(status_code=422, detail=f"room_data.json 파싱 실패: {e}")

    confirm_code = uuid.uuid4().hex[:6].upper()
    uploaded: list[str] = []

    # S3 origin/ 저장
    url = await upload_json(f"{confirm_code}/origin/room_data.json", raw_json)
    uploaded.append(url)

    room_usdz = next((n for n in names if n.endswith("Room.usdz")), None)
    if room_usdz:
        url = await upload_bytes(f"{confirm_code}/origin/Room.usdz", zf.read(room_usdz), "model/vnd.usdz+zip")
        uploaded.append(url)

    room_empty = next((n for n in names if n.endswith("Room_empty.usdz")), None)
    if room_empty:
        url = await upload_bytes(f"{confirm_code}/origin/Room_empty.usdz", zf.read(room_empty), "model/vnd.usdz+zip")
        uploaded.append(url)

    for model_path in (n for n in names if n.endswith(".usdc")):
        filename = model_path.split("/")[-1]
        url = await upload_bytes(f"{confirm_code}/origin/models/{filename}", zf.read(model_path), "application/octet-stream")
        uploaded.append(url)

    # DB 저장 (ORIGINAL)
    try:
        normalized = convert_roomplan_to_optimizer_payload(raw_json)
        layout_problem = build_layout_problem(normalized)
        await asyncio.to_thread(_save_original_to_db, confirm_code, uploaded[0], layout_problem)
    except Exception as e:
        logger.warning("DB 저장 실패 (계속 진행): %s", e)

    logger.info("Scan uploaded | confirm_code=%s", confirm_code)

    return ScanUploadResponse(
        message="scan uploaded",
        confirm_code=confirm_code,
        object_count=scan_data.objectCount,
        uploaded_files=uploaded,
    )


# ── POST /scans/{confirm_code}/optimize ──────────────────────────────────────
# 최적화 버튼: S3에서 원본 로드 → 최적화 → iOS 포맷으로 반환

@router.post("/{confirm_code}/optimize")
async def optimize_scan(confirm_code: str):
    # S3에서 원본 JSON 로드
    try:
        raw_json = await get_json(f"{confirm_code}/origin/room_data.json")
    except Exception as e:
        raise HTTPException(status_code=404, detail=f"스캔 데이터를 찾을 수 없습니다: {e}")

    # 변환 + 최적화
    try:
        normalized = convert_roomplan_to_optimizer_payload(raw_json)
        layout_problem = build_layout_problem(normalized)
        optimizer = CanonicalLayoutOptimizer(layout_problem)
        optimized_result = await asyncio.to_thread(optimizer.optimize)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"최적화 실패: {e}")

    # 역변환 → iOS 포맷 (room_data.roomplan_optimized.json)
    try:
        roomplan_optimized = export_optimized_layout_to_roomplan(raw_json, optimized_result)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"역변환 실패: {e}")

    # S3 processed/ 저장
    optimized_s3_url = await upload_json(f"{confirm_code}/processed/result.json", optimized_result)
    await upload_json(f"{confirm_code}/processed/room_data.roomplan_optimized.json", roomplan_optimized)

    # DB 저장 (OPTIMIZED)
    try:
        await asyncio.to_thread(_save_optimized_to_db, confirm_code, optimized_s3_url, optimized_result)
    except Exception as e:
        logger.warning("DB 저장 실패 (계속 진행): %s", e)

    # iOS가 바로 렌더링할 수 있는 포맷으로 반환
    return roomplan_optimized
