"""Exhibition challenge: visitors rearrange a room and compete on the layout score.

Flow: register (nickname + phone) -> load the start layout -> submit -> ranking.
Visitors have no V-O account; registration issues a token that only works for
that event. Scoring is server side (workers/optimizer/scoring.py), measured
against the event's AI layout.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import joinedload

from server.api.v1.deps import get_current_user
from server.api.v1.endpoints.rooms import build_version_detail
from server.api.v1.endpoints.rooms_common import _scan_root, _user_s3_segment
from server.api.v1.endpoints.rooms_edit import _load_json_from_s3_uri, build_edited_layouts
from server.core.s3 import upload_json
from server.schemas.event import (
    AttemptCreateRequest,
    AttemptResponse,
    ChallengeResponse,
    EventAdminResponse,
    EventCreateRequest,
    EventResponse,
    EventUpdateRequest,
    LeaderboardEntry,
    LeaderboardResponse,
    ParticipantCreateRequest,
    ParticipantCreateResponse,
)
from server.services.event_rules import (
    CRITERIA,
    DISQUALIFY_RULES,
    InvalidInput,
    hash_token,
    issue_participant_token,
    normalize_name,
    normalize_phone,
)
from server.services.event_ranking import best_attempts, participant_rank
from server.services.layout_scoring import compute_ai_record, room_frame, score_roomplan_layout
from shared.db import SessionLocal
from shared.models.event import Event, EventAttempt, EventParticipant
from shared.models.room import Room
from shared.models.user import User
from shared.models.version import Version
from workers.optimizer.scoring import MAX_TOTAL

logger = logging.getLogger(__name__)
router = APIRouter()

INACTIVE_DETAIL = "진행 중인 이벤트가 아니에요."
# A separate security scheme so Swagger's Authorize dialog has its own field for
# the participant token (it never shows a plain "Authorization" header input).
participant_scheme = HTTPBearer(
    auto_error=False,
    scheme_name="ParticipantToken",
    description="참가 등록 응답의 participant_token",
)


# ── helpers ───────────────────────────────────────────────────────────────────

def _event_or_404(db, event_id: int) -> Event:
    event = db.query(Event).filter(Event.id == event_id).first()
    if event is None:
        raise HTTPException(status_code=404, detail="이벤트를 찾을 수 없어요.")
    return event


def _ai_score(event: Event) -> float | None:
    return (event.ai_record or {}).get("total_score")


def _ai_usable(event: Event) -> float | None:
    record = event.ai_record or {}
    return None if record.get("disqualified") else record.get("usable_area_m2")


def _event_response(event: Event) -> EventResponse:
    return EventResponse(
        event_id=event.id,
        name=event.name,
        is_active=event.is_active,
        max_attempts=event.max_attempts,
        max_score=MAX_TOTAL,
        criteria=CRITERIA,
        disqualify_rules=DISQUALIFY_RULES,
    )


def _admin_response(event: Event) -> EventAdminResponse:
    return EventAdminResponse(
        **_event_response(event).model_dump(),
        room_id=event.room_id,
        start_version_id=event.start_version_id,
        target_version_id=event.target_version_id,
        ai_score=_ai_score(event),
    )


def _participant(
    event_id: int,
    credentials: HTTPAuthorizationCredentials | None = Security(participant_scheme),
) -> int:
    """Authenticate a visitor by the token issued at registration; returns the participant id."""
    token = credentials.credentials.strip() if credentials and credentials.credentials else None
    if not token:
        raise HTTPException(status_code=401, detail="참가 등록을 먼저 해주세요.")
    db = SessionLocal()
    try:
        participant = (
            db.query(EventParticipant)
            .filter(EventParticipant.token_hash == hash_token(token), EventParticipant.event_id == event_id)
            .first()
        )
        if participant is None:
            raise HTTPException(status_code=401, detail="참가 정보가 올바르지 않아요. 다시 등록해 주세요.")
        return participant.id
    finally:
        db.close()


def _attempt_response(db, event: Event, attempt: EventAttempt, remaining: int) -> AttemptResponse:
    rank, total = participant_rank(db, event.id, attempt.participant_id)
    return AttemptResponse(
        attempt_id=attempt.id,
        total_score=attempt.total_score,
        max_score=MAX_TOTAL,
        disqualified=attempt.disqualified,
        breakdown=attempt.breakdown,
        hints=attempt.hints or [],
        beat_ai=attempt.beat_ai,
        ai_score=_ai_score(event),
        rank=None if attempt.disqualified else rank,
        total_ranked=total,
        remaining_attempts=remaining,
    )


def _remaining(db, event: Event, participant_id: int) -> int:
    used = db.query(func.count(EventAttempt.id)).filter(EventAttempt.participant_id == participant_id).scalar()
    return max(event.max_attempts - used, 0)


# ── POST /events (방 소유자) ───────────────────────────────────────────────────

@router.post("", response_model=EventAdminResponse, status_code=201, summary="전시 이벤트 생성")
async def create_event(payload: EventCreateRequest, current_user: User = Depends(get_current_user)):
    db = SessionLocal()
    try:
        room = db.query(Room).filter(Room.id == payload.room_id, Room.user_id == current_user.id).first()
        if room is None:
            raise HTTPException(status_code=404, detail="방을 찾을 수 없어요.")
        versions = {
            v.id: v
            for v in db.query(Version).filter(
                Version.room_id == room.id,
                Version.id.in_([payload.start_version_id, payload.target_version_id]),
            )
        }
        start = versions.get(payload.start_version_id)
        target = versions.get(payload.target_version_id)
        if start is None or target is None:
            raise HTTPException(status_code=400, detail="시작 버전과 AI 버전은 이 방의 버전이어야 해요.")
        if not start.s3_json_url or not target.s3_json_url:
            raise HTTPException(status_code=400, detail="배치 JSON이 없는 버전은 쓸 수 없어요.")

        frame = await room_frame(db, room.id, _load_json_from_s3_uri)
        if frame is None:
            raise HTTPException(status_code=400, detail="원본 스캔이 없는 방은 채점할 수 없어요.")
        ai_record = await compute_ai_record(target, frame, _load_json_from_s3_uri)
        if ai_record["disqualified"]:
            raise HTTPException(
                status_code=400,
                detail="AI 버전 배치에 가구 겹침이나 방 밖으로 나간 가구가 있어 기준으로 쓸 수 없어요.",
            )

        event = Event(
            owner_user_id=current_user.id,
            room_id=room.id,
            start_version_id=start.id,
            target_version_id=target.id,
            name=payload.name.strip(),
            is_active=payload.is_active,
            max_attempts=payload.max_attempts,
            ai_record=ai_record,
        )
        db.add(event)
        db.commit()
        db.refresh(event)
        return _admin_response(event)
    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        logger.exception("Failed to create event for room_id=%s", payload.room_id)
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        db.close()


# ── PATCH /events/{event_id} (방 소유자) ───────────────────────────────────────

@router.patch("/{event_id}", response_model=EventAdminResponse, summary="전시 이벤트 설정 변경")
async def update_event(event_id: int, payload: EventUpdateRequest, current_user: User = Depends(get_current_user)):
    db = SessionLocal()
    try:
        event = _event_or_404(db, event_id)
        if event.owner_user_id != current_user.id:
            raise HTTPException(status_code=404, detail="이벤트를 찾을 수 없어요.")
        if payload.name is not None:
            event.name = payload.name.strip()
        if payload.max_attempts is not None:
            event.max_attempts = payload.max_attempts
        if payload.is_active is not None:
            event.is_active = payload.is_active
        db.commit()
        db.refresh(event)
        return _admin_response(event)
    finally:
        db.close()


# ── GET /events/{event_id} (공개) ─────────────────────────────────────────────

@router.get("/{event_id}", response_model=EventResponse, summary="전시 이벤트 정보와 채점 기준")
async def get_event(event_id: int):
    db = SessionLocal()
    try:
        return _event_response(_event_or_404(db, event_id))
    finally:
        db.close()


# ── POST /events/{event_id}/participants (공개) ───────────────────────────────

@router.post(
    "/{event_id}/participants",
    response_model=ParticipantCreateResponse,
    status_code=201,
    summary="참가 등록",
)
async def register_participant(event_id: int, payload: ParticipantCreateRequest):
    if not payload.privacy_consent:
        raise HTTPException(status_code=400, detail="개인정보 수집·이용에 동의해야 참여할 수 있어요.")
    try:
        name = normalize_name(payload.name)
        phone = normalize_phone(payload.phone)
    except InvalidInput as e:
        raise HTTPException(status_code=400, detail=str(e))

    db = SessionLocal()
    try:
        event = _event_or_404(db, event_id)
        if not event.is_active:
            raise HTTPException(status_code=409, detail=INACTIVE_DETAIL)
        token, token_hash = issue_participant_token()
        participant = EventParticipant(
            event_id=event.id,
            name=name,
            phone=phone,
            token_hash=token_hash,
            privacy_consent_at=datetime.now(timezone.utc),
        )
        db.add(participant)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            raise HTTPException(status_code=409, detail="이미 참여하신 번호예요.")
        db.refresh(participant)
        return ParticipantCreateResponse(
            participant_id=participant.id,
            participant_token=token,
            remaining_attempts=event.max_attempts,
        )
    finally:
        db.close()


# ── GET /events/{event_id}/challenge (참가자) ─────────────────────────────────

@router.get("/{event_id}/challenge", response_model=ChallengeResponse, summary="시작 배치 조회")
async def get_challenge(event_id: int, participant_id: int = Depends(_participant)):
    db = SessionLocal()
    try:
        event = _event_or_404(db, event_id)
        start = (
            db.query(Version)
            .options(joinedload(Version.room).joinedload(Room.user))
            .filter(Version.id == event.start_version_id)
            .first()
        )
        if start is None:
            raise HTTPException(status_code=404, detail="시작 배치를 찾을 수 없어요.")
        return ChallengeResponse(
            start=await build_version_detail(db, start),
            remaining_attempts=_remaining(db, event, participant_id),
        )
    finally:
        db.close()


# ── POST /events/{event_id}/attempts (참가자) ─────────────────────────────────

@router.post("/{event_id}/attempts", response_model=AttemptResponse, status_code=201, summary="배치 제출과 채점")
async def submit_attempt(
    event_id: int,
    payload: AttemptCreateRequest,
    participant_id: int = Depends(_participant),
    idempotency_key: str = Header(alias="Idempotency-Key", min_length=1, max_length=64),
):
    if not payload.ios_objects and not payload.objects:
        raise HTTPException(status_code=400, detail="배치한 가구 정보가 없어요.")

    db = SessionLocal()
    try:
        event = _event_or_404(db, event_id)
        # Lock the participant so parallel submissions cannot exceed the attempt limit.
        participant = (
            db.query(EventParticipant).filter(EventParticipant.id == participant_id).with_for_update().first()
        )
        previous = (
            db.query(EventAttempt)
            .filter(EventAttempt.participant_id == participant_id, EventAttempt.idempotency_key == idempotency_key)
            .first()
        )
        if previous is not None:
            # A retry of a submission that already went through: same answer again.
            return _attempt_response(db, event, previous, _remaining(db, event, participant_id))
        if not event.is_active:
            raise HTTPException(status_code=409, detail=INACTIVE_DETAIL)
        if _remaining(db, event, participant_id) <= 0:
            raise HTTPException(status_code=409, detail="참여 가능 횟수를 모두 사용했어요.")

        start = (
            db.query(Version)
            .options(joinedload(Version.room).joinedload(Room.user))
            .filter(Version.id == event.start_version_id)
            .first()
        )
        frame = await room_frame(db, event.room_id, _load_json_from_s3_uri)
        if start is None or frame is None:
            raise HTTPException(status_code=409, detail="이벤트 방 정보를 불러올 수 없어요.")

        ios_layout, _ = await build_edited_layouts(start, payload.ios_objects, payload.objects)
        score = await score_roomplan_layout(ios_layout, frame, _ai_usable(event))

        room = start.room
        key = (
            f"{_scan_root(str(room.id), _user_s3_segment(room.user))}"
            f"/events/{event.id}/attempt_{uuid.uuid4().hex}.roomplan.json"
        )
        layout_url = await upload_json(key, ios_layout)

        attempt = EventAttempt(
            event_id=event.id,
            participant_id=participant.id,
            idempotency_key=idempotency_key,
            total_score=score["total_score"],
            disqualified=score["disqualified"],
            breakdown=score["breakdown"],
            hints=score["hints"],
            usable_area_m2=score["usable_area_m2"],
            beat_ai=bool(score.get("beat_ai")),
            elapsed_seconds=payload.elapsed_seconds,
            layout_s3_url=layout_url,
        )
        db.add(attempt)
        db.commit()
        db.refresh(attempt)
        return _attempt_response(db, event, attempt, _remaining(db, event, participant_id))
    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        logger.exception("Failed to score attempt for event_id=%s participant_id=%s", event_id, participant_id)
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        db.close()


# ── GET /events/{event_id}/leaderboard (공개) ────────────────────────────────

@router.get("/{event_id}/leaderboard", response_model=LeaderboardResponse, summary="공개 순위표")
async def get_leaderboard(event_id: int, limit: int = Query(20, ge=1, le=100)):
    db = SessionLocal()
    try:
        event = _event_or_404(db, event_id)
        best = best_attempts(db, event.id)
        entries = [
            LeaderboardEntry(
                rank=position,
                name=attempt.participant.name,
                total_score=attempt.total_score,
                elapsed_seconds=attempt.elapsed_seconds,
                submitted_at=attempt.created_at,
            )
            for position, attempt in enumerate(best.limit(limit).all(), start=1)
        ]
        return LeaderboardResponse(
            event_id=event.id,
            total_ranked=best.count(),
            ai_score=_ai_score(event),
            entries=entries,
        )
    finally:
        db.close()


@router.get("/leaderboard/current", response_model=LeaderboardResponse, summary="현재 활성 이벤트 공개 순위표")
async def get_current_leaderboard(limit: int = Query(20, ge=1, le=100)):
    """Return the newest active event so the public website needs no hard-coded event id."""
    db = SessionLocal()
    try:
        event = db.query(Event).filter(Event.is_active.is_(True)).order_by(Event.id.desc()).first()
        if event is None:
            raise HTTPException(status_code=404, detail="진행 중인 이벤트가 없어요.")
        best = best_attempts(db, event.id)
        entries = [
            LeaderboardEntry(
                rank=position,
                name=attempt.participant.name,
                total_score=attempt.total_score,
                elapsed_seconds=attempt.elapsed_seconds,
                submitted_at=attempt.created_at,
            )
            for position, attempt in enumerate(best.limit(limit).all(), start=1)
        ]
        return LeaderboardResponse(
            event_id=event.id,
            total_ranked=best.count(),
            ai_score=_ai_score(event),
            entries=entries,
        )
    finally:
        db.close()
