"""AI 배치 상담 — Claude Messages API 중계.

API 키를 앱에 넣으면 앱을 뜯었을 때 그대로 노출되므로, 키는 서버에만 두고 앱은
로그인 토큰으로 이 엔드포인트를 호출한다. 모델과 토큰 상한은 서버가 정하고,
대화 내용과 tool 정의는 앱이 보낸 것을 그대로 전달한다.
"""
from __future__ import annotations

import logging

import httpx
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse

from server.api.v1.deps import get_current_user
from server.core.config import settings
from server.schemas.assistant import AssistantMessageRequest
from shared.models.user import User


logger = logging.getLogger(__name__)
router = APIRouter()

ANTHROPIC_MESSAGES_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"


@router.post("/messages", summary="AI 배치 상담 대화")
async def create_assistant_message(
    payload: AssistantMessageRequest,
    current_user: User = Depends(get_current_user),
):
    if not settings.anthropic_api_key:
        raise HTTPException(status_code=503, detail="AI 배치 상담을 사용할 수 없습니다.")

    body = {
        "model": settings.assistant_model,
        "max_tokens": settings.assistant_max_tokens,
        "system": payload.system,
        "messages": payload.messages,
        "tools": payload.tools,
    }

    try:
        async with httpx.AsyncClient(timeout=settings.assistant_timeout_seconds) as client:
            response = await client.post(
                ANTHROPIC_MESSAGES_URL,
                json=body,
                headers={
                    "x-api-key": settings.anthropic_api_key,
                    "anthropic-version": ANTHROPIC_VERSION,
                    "content-type": "application/json",
                },
            )
    except httpx.HTTPError as exc:
        logger.exception("Claude 요청 실패 user_id=%s", current_user.id)
        raise HTTPException(status_code=502, detail="AI 배치 상담 요청이 실패했습니다.") from exc

    if response.status_code >= 400:
        # 업스트림 오류 본문에는 키 관련 정보가 있을 수 있어 그대로 내려보내지 않는다.
        logger.error("Claude 응답 오류 %s: %s", response.status_code, response.text)
        raise HTTPException(status_code=502, detail="AI 배치 상담 요청이 실패했습니다.")

    return JSONResponse(status_code=200, content=response.json())
