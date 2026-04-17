import asyncio
import io
import json
import logging
import uuid
import zipfile
from typing import Any

from fastapi import APIRouter, File, HTTPException, UploadFile

from server.core.s3 import upload_bytes, upload_json, get_json
from server.schemas.scan import IOSRoomScanData, ScanUploadResponse
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
