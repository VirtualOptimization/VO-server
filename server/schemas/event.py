"""Exhibition challenge request/response schemas."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

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


class ChallengeResponse(BaseModel):
    start: VersionDetailResponse
    remaining_attempts: int


class AttemptCreateRequest(BaseModel):
    # Same shapes as POST /rooms/{room_id}/versions: iOS sends ios_objects, Unity sends objects.
    ios_objects: list[dict[str, Any]] | None = None
    objects: list[dict[str, Any]] = Field(default_factory=list)
    elapsed_seconds: int | None = Field(default=None, ge=0, le=24 * 3600)


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
