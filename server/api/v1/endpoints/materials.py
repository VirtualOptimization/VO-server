"""Natural-language material preview endpoints."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func
from sqlalchemy.orm import Session

from server.api.v1.deps import get_current_user
from server.core.config import settings
from server.core.s3 import build_s3_uri, generate_presigned_url_for_uri
from server.schemas.furniture import MaterialChatJobResponse, MaterialChatRequest, MaterialChatResponse
from server.services.material_chat_service import (
    MaterialChatRejected,
    generate_material_preview,
    interpret_material_request,
    s3_safe_segment,
)
from shared.db import SessionLocal
from shared.models.furniture_model import FurnitureModel
from shared.models.material_chat_job import MaterialChatJob
from shared.models.texture_preset import TexturePreset
from shared.models.user import User

router = APIRouter()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _to_response(job: MaterialChatJob) -> MaterialChatJobResponse:
    preset = job.texture_preset
    preview_url = None
    if preset:
        preview_url = generate_presigned_url_for_uri(build_s3_uri(preset.texture_s3_key))
    return MaterialChatJobResponse(
        status=job.status,
        message=job.assistant_message or job.error_message or "텍스처 요청을 처리 중입니다.",
        job_id=job.id,
        model_id=job.model_id,
        material_type=job.material_type,
        material_name=job.material_name,
        material_preset_id=preset.preset_key if preset else None,
        preview_url=preview_url,
        image_prompt=job.image_prompt,
        created_at=job.created_at,
        updated_at=job.updated_at,
    )


def _rate_limit_exceeded(db: Session, user_id: int, minutes: int, limit: int) -> bool:
    since = datetime.now(timezone.utc) - timedelta(minutes=minutes)
    request_count = (
        db.query(func.count(MaterialChatJob.id))
        .filter(MaterialChatJob.user_id == user_id, MaterialChatJob.created_at >= since)
        .scalar()
    )
    return int(request_count or 0) >= limit


@router.post("/chat", response_model=MaterialChatResponse, summary="AI 텍스처 대화 및 미리보기 생성")
async def create_material_preview(
    payload: MaterialChatRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    model = db.get(FurnitureModel, payload.model_id)
    if model is None or model.status == "DELETED" or model.user_id is not None:
        raise HTTPException(status_code=404, detail="텍스처 적용 가능한 기본 가구를 찾을 수 없습니다.")
    if _rate_limit_exceeded(db, current_user.id, 1, settings.material_chat_per_minute):
        raise HTTPException(
            status_code=429,
            detail=f"텍스처 요청은 1분에 {settings.material_chat_per_minute}회까지 가능합니다.",
        )
    if _rate_limit_exceeded(db, current_user.id, 24 * 60, settings.material_chat_per_day):
        raise HTTPException(
            status_code=429,
            detail=f"텍스처 요청은 하루 {settings.material_chat_per_day}회까지 가능합니다.",
        )

    job = MaterialChatJob(user_id=current_user.id, model_id=model.id, message=payload.message, status="PENDING")
    db.add(job)
    db.commit()
    db.refresh(job)

    try:
        interpretation = await interpret_material_request(payload.message)
        preset_key = f"ai_{job.id}_{uuid4().hex[:12]}"
        owner = s3_safe_segment(current_user.login_id)
        texture_key = f"textures/generated/{owner}/{preset_key}.png"
        await generate_material_preview(interpretation, s3_key=texture_key)

        preset = TexturePreset(
            user_id=current_user.id,
            preset_key=preset_key,
            name=interpretation.material_name,
            source="GENERATED",
            texture_s3_key=texture_key,
        )
        db.add(preset)
        db.flush()
        job.texture_preset_id = preset.id
        job.material_type = interpretation.material_type
        job.material_name = interpretation.material_name
        job.image_prompt = interpretation.image_prompt
        job.assistant_message = interpretation.assistant_message
        job.status = "PREVIEW_READY"
        db.commit()
        db.refresh(job)
        response = _to_response(job)
        return MaterialChatResponse(**response.model_dump(exclude={"created_at", "updated_at"}))
    except MaterialChatRejected as exc:
        job.status = "REJECTED"
        job.assistant_message = str(exc)
        db.commit()
        return MaterialChatResponse(status="REJECTED", message=str(exc), model_id=model.id)
    except Exception as exc:
        job.status = "FAILED"
        job.error_message = str(exc)[:1000]
        db.commit()
        raise HTTPException(status_code=502, detail="텍스처 미리보기 생성에 실패했습니다.") from exc


@router.get("/jobs/{job_id}", response_model=MaterialChatJobResponse, summary="AI 텍스처 미리보기 상태 조회")
def get_material_preview_job(
    job_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    job = db.get(MaterialChatJob, job_id)
    if job is None or job.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="텍스처 요청을 찾을 수 없습니다.")
    return _to_response(job)
