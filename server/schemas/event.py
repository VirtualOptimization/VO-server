"""Exhibition challenge request/response schemas."""

from __future__ import annotations

from datetime import datetime
import math
from typing import Any

from pydantic import BaseModel, Field, model_validator

from server.schemas.room_view import VersionDetailResponse


class EventCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    room_id: int
    start_version_id: int
    target_version_id: int
    max_attempts: int = Field(default=1, ge=1, le=20)
    is_active: bool = True


class EventUpdateRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    max_attempts: int | None = Field(default=None, ge=1, le=20)
    is_active: bool | None = None


class ScoreBreakdown(BaseModel):
    safety: float
    door_clearance: float
    furniture_use: float
    principles: float
    space: float
    penalty: float = 0.0


class Criterion(BaseModel):
    key: str
    title: str
    max_score: int
    description: str


class EventResponse(BaseModel):
    event_id: int
    name: str
    is_active: bool
    max_attempts: int
    max_score: int
    criteria: list[Criterion]
    disqualify_rules: list[str]
    penalty_rules: list[str]


class EventAdminResponse(EventResponse):
    room_id: int
    start_version_id: int
    target_version_id: int
    ai_score: float | None


class ParticipantCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=30)
    phone: str = Field(min_length=1, max_length=20)
    privacy_consent: bool


class ParticipantCreateResponse(BaseModel):
    participant_id: int
    participant_token: str
    remaining_attempts: int
    # True when the phone was already registered and this re-entered that entry.
    is_returning: bool = False


class ChallengeResponse(BaseModel):
    start: VersionDetailResponse
    remaining_attempts: int


class AttemptCreateRequest(BaseModel):
    # Same shapes as POST /rooms/{room_id}/versions: iOS sends ios_objects, Unity sends objects.
    ios_objects: list[dict[str, Any]] | None = None
    objects: list[dict[str, Any]] = Field(default_factory=list)
    elapsed_seconds: int | None = Field(default=None, ge=0, le=24 * 3600)

    @model_validator(mode="after")
    def validate_challenge_edits(self):
        # An explicit empty iOS list submits the unchanged starting layout.
        if self.ios_objects is None and not self.objects:
            raise ValueError("배치 정보를 보내주세요. 초기 배치는 ios_objects: []로 제출할 수 있어요.")
        if self.ios_objects is not None and self.objects:
            raise ValueError("한 번에 하나의 좌표계만 제출해 주세요.")
        updates = self.ios_objects if self.ios_objects is not None else self.objects
        seen = set()
        for item in updates:
            if set(item) - {"identifier", "center", "rotation", "transform"}:
                raise ValueError("챌린지에서는 기존 가구의 위치와 회전만 변경할 수 있어요.")
            identifier = item.get("identifier")
            if not isinstance(identifier, str) or not identifier.strip() or identifier in seen:
                raise ValueError("가구 identifier가 없거나 중복되었어요.")
            seen.add(identifier)
            if not any(key in item for key in ("center", "rotation", "transform")):
                raise ValueError("가구의 위치 또는 회전을 보내주세요.")
            for key, lengths in (("center", (3,)), ("rotation", (1, 3)), ("transform", (4,))):
                if key not in item:
                    continue
                value = item[key]
                if not isinstance(value, list) or len(value) not in lengths:
                    raise ValueError(f"{key} 형식이 올바르지 않아요.")
                if key == "transform":
                    if any(not isinstance(row, list) or len(row) != 4 for row in value):
                        raise ValueError("transform은 4×4 행렬이어야 해요.")
                    value = [number for row in value for number in row]
                if any(isinstance(n, bool) or not isinstance(n, (int, float)) or not math.isfinite(n) for n in value):
                    raise ValueError("가구 좌표는 유한한 숫자여야 해요.")
        return self


class AttemptResponse(BaseModel):
    attempt_id: int
    total_score: float
    max_score: int
    disqualified: bool
    breakdown: ScoreBreakdown | None
    hints: list[str]
    beat_ai: bool
    ai_score: float | None
    rank: int | None
    total_ranked: int
    remaining_attempts: int


class ParticipantSessionResponse(BaseModel):
    remaining_attempts: int
    latest_result: AttemptResponse | None
    elapsed_seconds: int | None


class MyLayoutResponse(BaseModel):
    """The participant's latest submission next to the starting layout, both renderable."""

    result: AttemptResponse
    submitted: VersionDetailResponse
    start: VersionDetailResponse


class LeaderboardEntry(BaseModel):
    rank: int
    name: str
    total_score: float
    elapsed_seconds: int | None
    submitted_at: datetime


class LeaderboardResponse(BaseModel):
    event_id: int
    total_ranked: int
    ai_score: float | None
    entries: list[LeaderboardEntry]
