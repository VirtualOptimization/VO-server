import logging

from fastapi import APIRouter

from server.core.s3 import upload_json
from server.schemas.scan import RoomScanRequest, RoomScanResponse

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post("", response_model=RoomScanResponse)
async def receive_scan(payload: RoomScanRequest):
    logger.info(
        "Received scan | room_id=%s, furniture_count=%d",
        payload.room_id,
        len(payload.furniture),
    )

    # TODO: 유효성 검사 (Lambda 연동 후 확장)
    # TODO: DB 저장 (A 스키마 확정 후 추가)

    s3_key = f"{payload.room_id}/origin/room_scan.json"
    s3_url = await upload_json(s3_key, payload.model_dump())

    return RoomScanResponse(
        message="scan received",
        room_id=payload.room_id,
        s3_key=s3_url,
    )
