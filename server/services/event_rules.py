"""Rules for the exhibition challenge that do not touch the database."""

from __future__ import annotations

import hashlib
import re
import secrets

from workers.optimizer.scoring import MAX_PRINCIPLES, MAX_SAFETY, MAX_SPACE, OUT_OF_ROOM_PENALTY, OVERLAP_PENALTY

NAME_MAX_LENGTH = 12
TOKEN_PREFIX = "pt_"

CRITERIA = [
    {
        "key": "safety",
        "title": "안전·동선",
        "max_score": MAX_SAFETY,
        "description": "문 앞을 막지 않고, 침대 옆·책상 앞처럼 가구를 쓸 공간이 있는지",
    },
    {
        "key": "principles",
        "title": "생활 원칙",
        "max_score": MAX_PRINCIPLES,
        "description": "침대 머리는 벽 쪽, 누웠을 때 문이 보이게 등 LH 주택 설계 기준의 배치 원칙",
    },
    {
        "key": "space",
        "title": "여유 공간",
        "max_score": MAX_SPACE,
        "description": "폭 80cm 이상으로 넓게 모인 빈 공간이 AI 배치와 비교해 얼마나 넓은지",
    },
]
# Kept in the response schema for older clients; no layout is scored 0 outright.
DISQUALIFY_RULES: list[str] = []
PENALTY_RULES = [
    f"겹친 가구 한 쌍마다 -{OVERLAP_PENALTY}점",
    f"방 밖으로 나간 가구 하나마다 -{OUT_OF_ROOM_PENALTY}점",
]


class InvalidInput(ValueError):
    """Input a visitor can fix; the message is shown to them as is."""


def normalize_name(name: str) -> str:
    cleaned = " ".join(name.split())
    if not cleaned:
        raise InvalidInput("닉네임을 입력해 주세요.")
    if len(cleaned) > NAME_MAX_LENGTH:
        raise InvalidInput(f"닉네임은 {NAME_MAX_LENGTH}자 이하로 입력해 주세요.")
    return cleaned


def normalize_phone(phone: str) -> str:
    digits = re.sub(r"\D", "", phone)
    if not re.fullmatch(r"0\d{9,10}", digits):
        raise InvalidInput("전화번호를 다시 확인해 주세요.")
    return digits


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def issue_participant_token() -> tuple[str, str]:
    """A new participant token and the hash stored for it (the token itself is never stored)."""
    token = TOKEN_PREFIX + secrets.token_urlsafe(32)
    return token, hash_token(token)
