"""공간 스캔 — 업로드 세션 시작 / 완료"""
from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from fastapi.responses import JSONResponse

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
from server.core.s3 import (
    build_s3_uri,
    delete_objects,
    generate_presigned_put_url,
    get_json,
    list_keys,
    object_exists,
    upload_json,
)
from server.services.local_scan_pipeline import (
    run_glb_conversion_background,
    run_local_optimize_pipeline,
)
from server.schemas.scan import (
    OptimizeStartResponse,
    PresignedUploadTarget,
    ScanUploadCompleteRequest,
    ScanUploadCompleteResponse,
    ScanCancelResponse,
    ScanUploadStartRequest,
    ScanUploadStartResponse,
)
from shared.db import SessionLocal
from shared.models.room import Room
from shared.models.user import User
from shared.models.version import Version

logger = logging.getLogger(__name__)
router = APIRouter()



# ── 내부 헬퍼 ────────────────────────────────────────────────────────────────

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
    name: str | None = None,
) -> int:
    db = SessionLocal()
    try:
        confirm_code = _generate_unique_confirm_code(db)
        room = Room(
            user_id=user_id,
            name=name.strip() if name and name.strip() else None,
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


def _mark_upload_completed(room_id: int, uploaded_keys: list[str]) -> int:
    """Save is a one-shot step: as soon as the original version exists, the room
    is done from the user's point of view and belongs in their room list.
    Optimization state is tracked separately on Room.optimization_status.
    """
    db = SessionLocal()
    try:
        room = db.get(Room, room_id)
        if not room:
            raise ValueError(f"Room not found: {room_id}")
        room.status = "COMPLETED"
        room_shell_key = next((k for k in uploaded_keys if k.endswith("/Room.usdz")), None)
        if room_shell_key:
            room.room_shell_usdc_url = build_s3_uri(room_shell_key)
        db.commit()
        return room.id
    finally:
        db.close()


def _get_room_context(room_id: int, user_id: int) -> tuple[str | None, str | None]:
    db = SessionLocal()
    try:
        room = db.query(Room).filter(Room.id == room_id, Room.user_id == user_id).first()
        if room is None:
            raise ValueError(f"Room not found: {room_id}")
        user = db.get(User, room.user_id)
        return _user_s3_segment(user), room.name
    finally:
        db.close()


def _claim_room_for_optimize(room_id: int, user_id: int, *, force: bool = False) -> dict[str, Any]:
    """Lock the room row and decide what POST /optimize should do.

    Locking (SELECT ... FOR UPDATE) and the status transition happen in one
    transaction so two concurrent optimize requests can't both start the
    pipeline for the same room.

    force=True re-runs even when a completed optimized version already
    exists (the user explicitly asked to discard it and try again). It never
    bypasses the PROCESSING guard -- a run already in flight must finish (or
    fail) before another one can start, to avoid two background tasks
    writing the same OPTIMIZED version row at once.
    """
    db = SessionLocal()
    try:
        room = (
            db.query(Room)
            .filter(Room.id == room_id, Room.user_id == user_id)
            .with_for_update()
            .first()
        )
        if room is None:
            return {"action": "not_found"}

        has_original = (
            db.query(Version.id)
            .filter(Version.room_id == room_id, Version.version_type == "ORIGINAL", Version.version_no == 0)
            .first()
            is not None
        )
        if not has_original:
            return {"action": "no_original"}

        if room.optimization_status == "PROCESSING":
            return {"action": "already_processing"}

        has_optimized = (
            db.query(Version.id)
            .filter(Version.room_id == room_id, Version.version_type == "OPTIMIZED")
            .first()
            is not None
        )
        if not force and room.optimization_status == "COMPLETED" and has_optimized:
            return {"action": "already_completed"}

        room.optimization_status = "PROCESSING"
        user = db.get(User, room.user_id)
        owner_segment = _user_s3_segment(user)
        db.commit()
        return {"action": "start", "owner_segment": owner_segment}
    except Exception:
        db.rollback()
        raise
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


def _ensure_original_version(
    room_id: int,
    raw_json_key: str,
    unity_room_data_key: str,
    room_name: str | None,
) -> None:
    db = SessionLocal()
    try:
        room = db.get(Room, room_id)
        if room is None:
            raise ValueError(f"Room not found: {room_id}")

        original_s3_url = build_s3_uri(raw_json_key)
        unity_s3_url = build_s3_uri(unity_room_data_key)
        original_json_data = {
            "source": "ios_upload",
            "room_data_json": original_s3_url,
            "unity_room_data_json": unity_s3_url,
        }

        original = (
            db.query(Version)
            .filter(
                Version.room_id == room_id,
                Version.version_type == "ORIGINAL",
                Version.version_no == 0,
            )
            .first()
        )
        if original:
            current_json_data = original.json_data if isinstance(original.json_data, dict) else {}
            original.s3_json_url = original_s3_url
            original.version_name = room_name
            original.json_data = {**current_json_data, **original_json_data}
        else:
            db.add(
                Version(
                    room_id=room_id,
                    parent_version_id=None,
                    version_type="ORIGINAL",
                    version_no=0,
                    version_name=room_name,
                    s3_json_url=original_s3_url,
                    json_data=original_json_data,
                )
            )
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


async def _create_origin_unity_json(raw_prefix: str) -> str:
    from server.services.transform.unity_roomplan import normalize_roomplan_for_unity

    room_data_key = f"{raw_prefix}/room_data.json"
    unity_room_data_key = f"{raw_prefix}/room_data.unity.json"
    room_data = await get_json(room_data_key)
    unity_room_data = normalize_roomplan_for_unity(room_data)
    await upload_json(unity_room_data_key, unity_room_data)
    return unity_room_data_key


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
        name=request.name,
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
    background_tasks: BackgroundTasks,
    current_user: User = Depends(get_current_user),
):
    """Save only: create the original version and mark the room complete.

    Optimization never runs here anymore — the room-view screen triggers it
    explicitly via POST /{room_id}/optimize once the user asks for it.
    """
    if not payload.uploaded_keys:
        raise HTTPException(status_code=422, detail="uploaded_keys는 비어 있을 수 없습니다.")

    try:
        owner_segment, room_name = _get_room_context(room_id, current_user.id)
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

    room_id = await asyncio.to_thread(_mark_upload_completed, room_id, payload.uploaded_keys)
    await asyncio.to_thread(_ensure_original_version, room_id, required_json_key, unity_room_data_key, room_name)
    processed_keys = [*payload.uploaded_keys, unity_room_data_key]
    pipeline_input = _build_pipeline_input(room_id, processed_keys, owner_segment)

    # Unity가 방 껍데기 GLB를 쓰므로 변환은 필요하지만, 저장 응답을 막을 이유는
    # 없다. 백그라운드로 돌리고 결과를 기다리지 않는다.
    background_tasks.add_task(run_glb_conversion_background, pipeline_input)

    return ScanUploadCompleteResponse(
        message="scan upload completed", room_id=room_id,
        raw_prefix=raw_prefix, generated_prefix=generated_prefix,
        uploaded_keys=processed_keys, pipeline_started=False,
        execution_arn=None, pipeline_input=pipeline_input,
    )


# ── POST /rooms/{room_id}/optimize ───────────────────────────────────────────

@router.post("/{room_id}/optimize", response_model=OptimizeStartResponse)
async def optimize_room(
    room_id: int,
    background_tasks: BackgroundTasks,
    force: bool = False,
    current_user: User = Depends(get_current_user),
):
    """Start (or report the status of) furniture-layout optimization for a saved room.

    Requires no request body: unlike /complete, iOS does not send uploaded_keys
    here, so the S3 objects under the room's raw prefix are checked directly.

    By default, calling this again after optimization already completed just
    reports COMPLETED without re-running (cheap to poll, doesn't burn AI/compute
    cost on accidental double-taps). Pass ?force=true to discard the existing
    optimized version and run again -- e.g. a "다시 최적화하기" / redo action.
    """
    claim = await asyncio.to_thread(_claim_room_for_optimize, room_id, current_user.id, force=force)
    action = claim["action"]

    if action == "not_found":
        raise HTTPException(status_code=404, detail="방을 찾을 수 없습니다.")
    if action == "no_original":
        raise HTTPException(status_code=409, detail="원본 버전이 없습니다.")
    if action == "already_processing":
        return JSONResponse(
            status_code=202,
            content=OptimizeStartResponse(room_id=room_id, optimization_status="PROCESSING").model_dump(),
        )
    if action == "already_completed":
        return JSONResponse(
            status_code=200,
            content=OptimizeStartResponse(room_id=room_id, optimization_status="COMPLETED").model_dump(),
        )

    owner_segment = claim["owner_segment"]
    raw_prefix = _raw_prefix(str(room_id), owner_segment)

    candidate_keys = [
        f"{raw_prefix}/room_data.json",
        f"{raw_prefix}/Room.usdz",
        f"{raw_prefix}/Room_empty.usdz",
    ]
    exists_flags = await asyncio.gather(*(object_exists(key) for key in candidate_keys))
    uploaded_keys = [key for key, exists in zip(candidate_keys, exists_flags) if exists]
    unity_room_data_key = f"{raw_prefix}/room_data.unity.json"
    if await object_exists(unity_room_data_key):
        uploaded_keys.append(unity_room_data_key)

    pipeline_input = _build_pipeline_input(room_id, uploaded_keys, owner_segment)
    background_tasks.add_task(run_local_optimize_pipeline, pipeline_input)

    return JSONResponse(
        status_code=202,
        content=OptimizeStartResponse(room_id=room_id, optimization_status="PROCESSING").model_dump(),
    )


@router.delete("/{room_id}", response_model=ScanCancelResponse, summary="방 삭제")
async def delete_room(
    room_id: int,
    current_user: User = Depends(get_current_user),
):
    """Delete a room with all of its versions and every S3 object created for it.

    Covers both an abandoned upload session (the room row exists before the files
    land) and a saved room the user no longer wants. Deleting while a worker is
    still writing would race with it, so processing rooms are rejected instead.
    """
    db = SessionLocal()
    try:
        room = db.query(Room).filter(Room.id == room_id, Room.user_id == current_user.id).first()
        if room is None:
            raise HTTPException(status_code=404, detail="방을 찾을 수 없습니다.")
        if room.status == "PROCESSING" or room.optimization_status == "PROCESSING":
            raise HTTPException(
                status_code=409,
                detail="처리 중인 방은 삭제할 수 없습니다. 잠시 후 다시 시도해주세요.",
            )

        owner_segment = _user_s3_segment(current_user)
        prefixes = (
            _raw_prefix(str(room.id), owner_segment),
            _generated_prefix(str(room.id), owner_segment),
        )
        keys = await asyncio.to_thread(
            lambda: [key for prefix in prefixes for key in list_keys(prefix)]
        )
        if keys:
            await asyncio.to_thread(delete_objects, keys)

        db.delete(room)
        db.commit()
        return ScanCancelResponse(
            room_id=room_id,
            status="DELETED",
            deleted_s3_object_count=len(keys),
        )
    except HTTPException:
        raise
    except Exception as exc:
        db.rollback()
        logger.exception("Failed to delete room room_id=%s", room_id)
        raise HTTPException(status_code=500, detail="방을 삭제하지 못했습니다.") from exc
    finally:
        db.close()
