import logging
import uuid

from fastapi import APIRouter, HTTPException

from server.core.s3 import upload_json
from server.schemas.scan import IOSRoomScanData, ScanUploadResponse

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post("", response_model=ScanUploadResponse)
async def receive_scan(payload: IOSRoomScanData):
    """iOS RoomPlan JSON 수신 → S3 원본 저장"""

    logger.info("Received scan | scannedAt=%s, objects=%d", payload.scannedAt, payload.objectCount)

    # confirm_code 생성 (임시 — 추후 A의 Lambda로 교체)
    confirm_code = uuid.uuid4().hex[:6].upper()

    # S3에 원본 JSON 저장
    s3_key = f"{confirm_code}/origin/room_data.json"
    try:
        s3_url = await upload_json(s3_key, payload.model_dump())
    except Exception as e:
        logger.error("S3 업로드 실패: %s", e)
        raise HTTPException(status_code=500, detail=f"S3 업로드 실패: {e}")

    # TODO: A의 변환 로직 연결 (workers/json_transform)
    # TODO: DB 저장 (A 스키마 확정 후)

    return ScanUploadResponse(
        message="scan received",
        confirm_code=confirm_code,
        object_count=payload.objectCount,
        s3_url=s3_url,
    )
