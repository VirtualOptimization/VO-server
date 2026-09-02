"""Gemini-backed material chat helpers.

The chat service only creates a texture preview. Applying that texture to a GLB
remains part of the existing USER_EDITED version save flow.
"""

from __future__ import annotations

import asyncio
import base64
import json
import re
import ssl
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

import certifi

from server.core.config import settings
from server.core.s3 import upload_bytes


SUPPORTED_MATERIAL_TYPES = {
    "wood",
    "marble",
    "rattan",
    "fabric",
    "metal",
    "leather",
    "stone",
    "ceramic",
    "paint",
}
MATERIAL_ALIASES = {
    "나무": "wood",
    "우드": "wood",
    "목재": "wood",
    "대리석": "marble",
    "라탄": "rattan",
    "패브릭": "fabric",
    "천": "fabric",
    "금속": "metal",
    "가죽": "leather",
    "돌": "stone",
    "석재": "stone",
    "세라믹": "ceramic",
    "페인트": "paint",
}
BLOCKED_TERMS = {"폭탄", "무기", "마약", "혐오", "자해", "살해"}
OUT_OF_SCOPE_TERMS = {"날씨", "주식", "코딩", "번역", "여행", "레시피", "뉴스"}


@dataclass(frozen=True)
class MaterialInterpretation:
    material_type: str
    material_name: str
    image_prompt: str
    assistant_message: str


class MaterialChatRejected(ValueError):
    """The message should not generate a furniture texture."""


def validate_material_message(message: str) -> None:
    normalized = message.lower()
    if any(term in normalized for term in BLOCKED_TERMS):
        raise MaterialChatRejected("안전 정책상 해당 요청으로 텍스처를 만들 수 없습니다.")
    if any(term in normalized for term in OUT_OF_SCOPE_TERMS):
        raise MaterialChatRejected("가구 표면의 색상·무늬·재질을 설명해 주세요.")


async def interpret_material_request(message: str) -> MaterialInterpretation:
    """Convert a user's Korean request into a constrained texture prompt."""
    validate_material_message(message)
    if not settings.material_chat_enabled:
        raise RuntimeError("MATERIAL_CHAT_ENABLED=true 설정이 필요합니다.")
    if not settings.gemini_api_key:
        raise RuntimeError("GEMINI_API_KEY 설정이 필요합니다.")

    payload = await asyncio.to_thread(_request_material_interpretation, message)
    return _validate_interpretation(payload, message)


async def generate_material_preview(
    interpretation: MaterialInterpretation,
    *,
    s3_key: str,
) -> str:
    """Generate a square material tile with Gemini and store it in S3."""
    image_bytes, content_type = await generate_material_preview_bytes(interpretation)
    return await upload_bytes(s3_key, image_bytes, content_type)


async def generate_material_preview_bytes(
    interpretation: MaterialInterpretation,
) -> tuple[bytes, str]:
    """Generate a preview without persisting it to S3."""
    return await asyncio.to_thread(_request_material_image, interpretation.image_prompt)


def _request_material_interpretation(message: str) -> dict[str, Any]:
    schema = {
        "material_type": "wood | marble | rattan | fabric | metal | leather | stone | ceramic | paint",
        "material_name": "short Korean display name",
        "image_prompt": "English prompt for a seamless 1:1 PBR base-color material tile",
        "assistant_message": "short Korean confirmation",
    }
    prompt = (
        "You are a furniture material assistant. Return only JSON, without markdown. "
        "The user is requesting a texture for a furniture surface. Interpret only the requested material, "
        "not furniture placement or general conversation. Choose one supported material type. "
        "The image_prompt must describe a seamless, tileable, front-facing, evenly lit material texture. "
        "Do not include furniture, room scenes, logos, text, labels, people, or shadows. "
        f"Schema: {json.dumps(schema, ensure_ascii=False)}\n"
        f"User request: {message}"
    )
    response = _gemini_generate(
        model=settings.gemini_material_text_model,
        body={
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {"responseMimeType": "application/json"},
        },
    )
    return _loads_json_object(_extract_text(response))


def _request_material_image(image_prompt: str) -> tuple[bytes, str]:
    prompt = (
        "Generate only a seamless square PBR base-color texture tile for a furniture surface. "
        "No object, room, text, logo, border, watermark, lighting gradient, or cast shadow. "
        f"Material request: {image_prompt}"
    )
    response = _gemini_generate(
        model=settings.gemini_material_image_model,
        body={
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {
                "responseModalities": ["IMAGE"],
            },
        },
    )
    return _extract_inline_image(response)


def _gemini_generate(*, model: str, body: dict[str, Any]) -> dict[str, Any]:
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={settings.gemini_api_key}"
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        ssl_context = ssl.create_default_context(cafile=certifi.where())
        with urllib.request.urlopen(
            request,
            timeout=settings.material_chat_timeout_seconds,
            context=ssl_context,
        ) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Gemini API error {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Gemini API 연결에 실패했습니다: {exc.reason}") from exc


def _extract_text(payload: dict[str, Any]) -> str:
    for candidate in payload.get("candidates") or []:
        parts = candidate.get("content", {}).get("parts", [])
        texts = [str(part["text"]) for part in parts if part.get("text")]
        if texts:
            return "\n".join(texts).strip()
    raise RuntimeError("Gemini 응답에 텍스트 결과가 없습니다.")


def _extract_inline_image(payload: dict[str, Any]) -> tuple[bytes, str]:
    for candidate in payload.get("candidates") or []:
        for part in candidate.get("content", {}).get("parts", []):
            inline_data = part.get("inlineData") or part.get("inline_data")
            if not isinstance(inline_data, dict) or not inline_data.get("data"):
                continue
            content_type = str(inline_data.get("mimeType") or inline_data.get("mime_type") or "image/png")
            if not content_type.startswith("image/"):
                continue
            return base64.b64decode(inline_data["data"]), content_type
    raise RuntimeError("Gemini 응답에 생성된 텍스처 이미지가 없습니다.")


def _loads_json_object(text: str) -> dict[str, Any]:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.strip("`").removeprefix("json").strip()
    start, end = stripped.find("{"), stripped.rfind("}")
    if start < 0 or end < start:
        raise RuntimeError("Gemini 응답이 JSON 형식이 아닙니다.")
    return json.loads(stripped[start : end + 1])


def _validate_interpretation(payload: dict[str, Any], fallback_message: str) -> MaterialInterpretation:
    material_type = str(payload.get("material_type") or "").strip().lower()
    material_type = MATERIAL_ALIASES.get(material_type, material_type)
    if material_type not in SUPPORTED_MATERIAL_TYPES:
        material_type = _guess_material_type(fallback_message)

    material_name = str(payload.get("material_name") or material_type).strip()[:100]
    image_prompt = str(payload.get("image_prompt") or "").strip()[:800]
    if not image_prompt:
        image_prompt = f"seamless {material_type} texture, furniture material, neutral lighting"
    assistant_message = str(payload.get("assistant_message") or "텍스처 미리보기를 만들었습니다.").strip()[:300]
    return MaterialInterpretation(material_type, material_name, image_prompt, assistant_message)


def _guess_material_type(message: str) -> str:
    lowered = message.lower()
    for alias, material_type in MATERIAL_ALIASES.items():
        if alias in lowered:
            return material_type
    for material_type in SUPPORTED_MATERIAL_TYPES:
        if material_type in lowered:
            return material_type
    return "wood"


def s3_safe_segment(value: str) -> str:
    segment = re.sub(r"[^A-Za-z0-9_-]+", "-", value).strip("-")
    return segment or "user"
