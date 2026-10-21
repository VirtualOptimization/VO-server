"""Challenge HTTP contracts with a real transactional test database and mocked S3/scoring.

SQLite exercises routing, ownership, ranking and persistence. PostgreSQL's
FOR UPDATE serialization still needs its deployment integration check.
"""
import copy
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from server.api.v1.endpoints import events
from shared.models import Event, EventAttempt, EventParticipant, Room, User, Version


@compiles(JSONB, "sqlite")
def _jsonb_as_json(type_, compiler, **kwargs):
    return "JSON"


@pytest.fixture
def challenge(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    tables = [model.__table__ for model in (User, Room, Version, Event, EventParticipant, EventAttempt)]
    User.metadata.create_all(engine, tables=tables)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(events, "SessionLocal", factory)
    with factory() as db:
        db.add(User(id=1, login_id="owner", password_hash="unused"))
        db.add(Room(id=1, user_id=1, confirm_code="123456"))
        db.add_all([Version(id=i, room_id=1, version_type="ORIGINAL" if i == 1 else "OPTIMIZED",
                            version_no=i, s3_json_url=f"s3://test/{i}.json") for i in (1, 2)])
        db.add(Event(id=1, owner_user_id=1, room_id=1, start_version_id=1, target_version_id=2,
                     name="Challenge", is_active=True, max_attempts=2,
                     ai_record={"total_score":80, "usable_area_m2":2}))
        db.commit()
    layout = {"objects": [{"identifier":"bed"}]}
    monkeypatch.setattr(events, "_load_json_from_s3_uri", AsyncMock(return_value=layout))
    monkeypatch.setattr(events, "room_frame", AsyncMock(return_value={"frame": True}))
    monkeypatch.setattr(events, "build_edited_layouts", AsyncMock(return_value=(layout, {})))
    monkeypatch.setattr(events, "upload_json", AsyncMock(return_value="s3://test/submission.json"))
    monkeypatch.setattr(events, "score_roomplan_layout", AsyncMock(return_value={
        "total_score":90, "disqualified":False, "breakdown":{"safety":30,"door_clearance":15,
        "furniture_use":15,"principles":30,"space":30}, "hints":[], "usable_area_m2":2.2, "beat_ai":True}))
    monkeypatch.setattr(events, "build_version_detail", AsyncMock(return_value={
        "version_id":1,"room_id":1,"version_type":"ORIGINAL","version_no":1,
        "layout_json_url":"https://test/layout.json"}))
    app = FastAPI()
    app.include_router(events.router, prefix="/api/events")
    with TestClient(app) as client:
        yield client, factory
    engine.dispose()


def register(client, phone="01012345678"):
    response = client.post("/api/events/1/participants", json={"name":"visitor", "phone":phone, "privacy_consent":True})
    assert response.status_code == 201
    return {"Authorization":f"Bearer {response.json()['participant_token']}"}


def submit(client, headers, key="one", payload=None):
    return client.post("/api/events/1/attempts", headers={**headers,"Idempotency-Key":key},
                       json=payload if payload is not None else {"ios_objects":[],"elapsed_seconds":65})


def test_current_event_and_leaderboard_use_the_same_latest_active_event(challenge):
    client, factory = challenge
    with factory() as db:
        db.add_all([Event(id=i, owner_user_id=1, room_id=1, start_version_id=1,target_version_id=2,
                         name=f"Event {i}",is_active=i == 2,max_attempts=1) for i in (2,3)])
        db.commit()
    assert client.get("/api/events/current").json()["event_id"] == 2
    assert client.get("/api/events/leaderboard/current").json()["event_id"] == 2
    with factory() as db:
        db.query(Event).update({Event.is_active:False}); db.commit()
    assert client.get("/api/events/current").status_code == 404


def test_registration_privacy_duplicate_and_session_auth(challenge):
    client, _ = challenge
    assert client.post("/api/events/1/participants",json={"name":"x","phone":"01012345678","privacy_consent":False}).status_code == 400
    headers = register(client)
    assert client.post("/api/events/1/participants",json={"name":"x","phone":"010-1234-5678","privacy_consent":True}).status_code == 409
    assert client.get("/api/events/1/session").status_code == 401
    assert client.get("/api/events/2/session",headers=headers).status_code == 401
    assert client.get("/api/events/1/session",headers=headers).json() == {
        "remaining_attempts":2,"latest_result":None,"elapsed_seconds":None}


def test_unchanged_layout_is_scored_once_and_can_be_recovered(challenge):
    client, factory = challenge
    headers = register(client)
    response = submit(client,headers)
    assert response.status_code == 201, response.text
    first = response.json()
    assert first["remaining_attempts"] == 1
    assert first["rank"] == 1
    assert submit(client,headers).json()["attempt_id"] == first["attempt_id"]
    assert events.score_roomplan_layout.await_count == 1
    events.build_edited_layouts.assert_not_awaited()
    status = client.get("/api/events/1/session",headers=headers).json()
    assert status["latest_result"]["attempt_id"] == first["attempt_id"]
    assert status["elapsed_seconds"] == 65
    assert submit(client,headers,"two").json()["remaining_attempts"] == 0
    assert submit(client,headers,"three").status_code == 409
    with factory() as db:
        assert db.query(EventAttempt).count() == 2
    board = client.get("/api/events/1/leaderboard").json()
    assert board["total_ranked"] == 1
    assert "phone" not in str(board) and "participant_token" not in str(board)


def test_completed_submission_can_be_retried_after_event_closes(challenge):
    client, factory = challenge
    headers = register(client)
    attempt = submit(client,headers).json()["attempt_id"]
    with factory() as db:
        db.get(Event,1).is_active = False; db.commit()
    assert client.get("/api/events/1/challenge",headers=headers).status_code == 409
    assert submit(client,headers).json()["attempt_id"] == attempt
    assert submit(client,headers,"new").status_code == 409


@pytest.mark.parametrize("payload", [
    {}, {"ios_objects":[{"identifier":"bed","dimensions":[0.1,0.1,0.1]}]},
    {"ios_objects":[{"identifier":"bed","operation":"create","center":[1,2,3]}]},
    {"ios_objects":[{"identifier":"bed","transform":[[1]]}]},
    {"ios_objects":[{"identifier":"bed","center":[True,0,0]}]},
    {"ios_objects":[{"identifier":"bed","center":[0,0,0]},{"identifier":"bed","center":[1,0,1]}]},
    {"ios_objects":[],"objects":[{"identifier":"bed","center":[1,0,1]}]},
])
def test_forged_or_ambiguous_payloads_are_rejected_before_scoring(challenge,payload):
    client, _ = challenge
    assert submit(client,register(client),payload=payload).status_code == 422
    events.score_roomplan_layout.assert_not_awaited()


def test_rank_uses_best_valid_attempt_and_has_a_deterministic_final_tie_break(challenge):
    client, factory = challenge
    first = register(client)
    second = register(client,"01099998888")
    now = datetime(2026,1,1,tzinfo=timezone.utc)
    with factory() as db:
        participants = db.query(EventParticipant).order_by(EventParticipant.id).all()
        for key, pid, score, area, elapsed, dq in [
            ('a',participants[0].id,90,3,60,False), ('b',participants[0].id,95,3,60,True),
            ('c',participants[0].id,80,2,50,False), ('d',participants[1].id,90,3,60,False)]:
            db.add(EventAttempt(event_id=1,participant_id=pid,idempotency_key=key,total_score=score,
                                disqualified=dq,usable_area_m2=area,elapsed_seconds=elapsed,created_at=now,beat_ai=False))
        db.commit()
    board = client.get("/api/events/1/leaderboard").json()
    assert board["total_ranked"] == 2
    assert [row["rank"] for row in board["entries"]] == [1,2]
    assert [row["total_score"] for row in board["entries"]] == [90,90]
    assert client.get("/api/events/1/session",headers=first).json()["latest_result"]["rank"] == 1
    assert client.get("/api/events/1/session",headers=second).json()["latest_result"]["rank"] == 2


def test_event_closing_during_scoring_prevents_a_new_attempt(challenge, monkeypatch):
    client, factory = challenge
    headers = register(client)
    result = copy.deepcopy(events.score_roomplan_layout.return_value)
    async def score_then_close(*args):
        with factory() as db:
            db.get(Event, 1).is_active = False
            db.commit()
        return result
    monkeypatch.setattr(events, "score_roomplan_layout", score_then_close)
    assert submit(client, headers).status_code == 409
    with factory() as db:
        assert db.query(EventAttempt).count() == 0
