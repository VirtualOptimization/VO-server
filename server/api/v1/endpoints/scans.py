import asyncio
import io
import json
import logging
import uuid
import zipfile
from typing import Any

import boto3
from fastapi import APIRouter, File, HTTPException, UploadFile, Depends
from pydantic import BaseModel, Field

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
URL_EXPIRATION_SECONDS = 3600

stepfunctions_client = boto3.client(
    "stepfunctions",
    region_name=settings.aws_region,
    aws_access_key_id=settings.aws_access_key_id,
    aws_secret_access_key=settings.aws_secret_access_key,
)


class ScanUploadCompleteRequest(BaseModel):
    uploaded_keys: list[str] = Field(default_factory=list)
    room_scan_data: IOSRoomScanData  


def _raw_prefix(confirm_code: str) -> str:
    return f"{RAW_ROOT_PREFIX}/{confirm_code}/origin"


def _generated_prefix(confirm_code: str) -> str:
    return f"{RAW_ROOT_PREFIX}/{confirm_code}/optimized"


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


def _build_step_functions_input(confirm_code: str) -> dict[str, Any]:
    raw_prefix = _raw_prefix(confirm_code)
    return {
        "confirm_code": confirm_code,
        "bucket": settings.s3_bucket_name,
        "inputs": {
            "room_usdz": f"{raw_prefix}/Room.usdz",
            "room_empty_usdz": f"{raw_prefix}/Room_empty.usdz",
            "models_prefix": f"{raw_prefix}/Models/"
        },
        "outputs": _build_generated_outputs(confirm_code)
    }


def _save_optimized_to_db(room_id: int, optimized_data: dict[str, Any]) -> None:
    db = SessionLocal()
    try:
        existing_version = db.query(Version).filter(
            Version.room_id == room_id,
            Version.version_type == "OPTIMIZED",
            Version.version_no == 1
        ).first()

        if existing_version:
            logger.info(f"Existing OPTIMIZED v1 found for room_id {room_id}. Cleaning children and updating...")
            # 🎯 space_id -> version_id로 변경
            db.query(FurnitureItem).filter(FurnitureItem.version_id == existing_version.id).delete()
            version = existing_version
            version.json_data = optimized_data
        else:
            logger.info(f"Creating new OPTIMIZED v1 for room_id {room_id}.")
            version = Version(
                room_id=room_id,
                version_no=1,
                version_type="OPTIMIZED",
                json_data=optimized_data
            )
            db.add(version)
            db.flush() 

        elements = optimized_data.get("elements", [])
        for elem in elements:
            transform_dict = elem.get("transform_dict", {})
            
            furniture = FurnitureItem(
                version_id=version.id,  # 🎯 space_id -> version_id로 변경
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

    if request.include_room_usdz:
        s3_key = f"{raw_prefix}/Room.usdz"
        targets.append(PresignedUploadTarget(
            logical_name="room_usdz",
            s3_key=s3_key,
            presigned_url=await generate_presigned_put_url(s3_key, USDZ_CONTENT_TYPE),
            content_type=USDZ_CONTENT_TYPE
        ))

    if request.include_room_empty_usdz:
        s3_key = f"{raw_prefix}/Room_empty.usdz"
        targets.append(PresignedUploadTarget(
            logical_name="room_empty_usdz",
            s3_key=s3_key,
            presigned_url=await generate_presigned_put_url(s3_key, USDZ_CONTENT_TYPE),
            content_type=USDZ_CONTENT_TYPE
        ))

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
async def complete_scan_upload(confirm_code: str, request: ScanUploadCompleteRequest):
    db = SessionLocal()
    try:
        room = db.query(Room).filter(Room.confirm_code == confirm_code).first()
        if not room:
            raise HTTPException(status_code=404, detail="Room session not found.")

        raw_prefix = _raw_prefix(confirm_code)

        for key in request.uploaded_keys:
            if not key.startswith(f"{raw_prefix}/"):
                raise HTTPException(
                    status_code=400,
                    detail=f"Security/Validation Integrity Error: Uploaded key '{key}' escapes designated S3 safe boundary."
                )

        s3_json_key = f"{raw_prefix}/room_scan.json"
        s3_json_url = await upload_json(s3_json_key, request.room_scan_data.model_dump())

        existing_v0 = db.query(Version).filter(
            Version.room_id == room.id,
            Version.version_type == "ORIGINAL",
            Version.version_no == 0
        ).first()

        if existing_v0:
            db.query(FurnitureItem).filter(FurnitureItem.version_id == existing_v0.id).delete()
            v0_version = existing_v0
            v0_version.s3_json_url = s3_json_url
            v0_version.json_data = request.room_scan_data.model_dump()
        else:
            v0_version = Version(
                room_id=room.id,
                version_no=0,
                version_type="ORIGINAL",
                s3_json_url=s3_json_url,
                json_data=request.room_scan_data.model_dump()
            )
            db.add(v0_version)
            db.flush()

        for obj in request.room_scan_data.objects:
            center = obj.center if len(obj.center) == 3 else [0.0, 0.0, 0.0]
            dimensions = obj.dimensions if len(obj.dimensions) == 3 else [1.0, 1.0, 1.0]
            
            furniture = FurnitureItem(
                version_id=v0_version.id,  
                model_id=None,
                item_key=obj.identifier,
                
                pos_x=center[0],
                pos_y=center[1],
                pos_z=center[2],
                rot_x=0.0,
                rot_y=0.0,
                rot_z=0.0,
                scale_x=dimensions[0],
                scale_y=dimensions[1],
                scale_z=dimensions[2],
                footprint_polygon=None
            )
            db.add(furniture)

        room.status = "PROCESSING"
        db.commit()
        
        room_id = room.id
        generated_prefix = _generated_prefix(confirm_code)
        
    except Exception as e:
        db.rollback()
        logger.error(f"Complete flow transaction runtime crash: {str(e)}")
        raise
    finally:
        db.close()

    pipeline_input = _build_step_functions_input(confirm_code)
    
    try:
        response = stepfunctions_client.start_execution(
            stateMachineArn=settings.step_functions_state_machine_arn,
            name=f"VO-Scan-{confirm_code}-{uuid.uuid4().hex[:8].upper()}",
            input=json.dumps(pipeline_input)
        )
        
        return ScanUploadCompleteResponse(
            message="Step Functions orchestration pipeline triggered successfully. Original v0 scan layout archived.",
            room_id=room_id,
            confirm_code=confirm_code,
            raw_prefix=raw_prefix,
            generated_prefix=generated_prefix,
            uploaded_keys=request.uploaded_keys,
            pipeline_started=True,
            execution_arn=response["executionArn"],
            pipeline_input=pipeline_input
        )
        
    except Exception as e:
        logger.error(f"Failed to switch AWS Step Functions trigger pipeline: {str(e)}")
        raise HTTPException(
            status_code=500, 
            detail="Backend internal step functions trigger failed."
        )