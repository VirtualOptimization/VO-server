"""Score saved layouts for the exhibition challenge (see workers/optimizer/scoring.py)."""

from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable

from sqlalchemy.orm import Session

from server.services.transform import build_layout_problem, convert_roomplan_to_optimizer_payload
from shared.models.version import Version
from workers.optimizer.scoring import score_layout

# Bump when the scoring rules change; stored with each AI record.
SCORE_VERSION = 4

LoadJson = Callable[[str | None, str], Awaitable[dict]]


def _score_sync(layout: dict[str, Any], frame: dict[str, Any], reference_usable_m2: float | None) -> dict[str, Any]:
    # The room frame comes from the original scan so furniture dragged past a
    # wall cannot enlarge the room.
    problem = build_layout_problem(convert_roomplan_to_optimizer_payload(layout, frame_src=frame))
    return score_layout(problem, reference_usable_m2)


async def score_roomplan_layout(
    layout: dict[str, Any], frame: dict[str, Any], reference_usable_m2: float | None
) -> dict[str, Any]:
    """Score an iOS-frame RoomPlan layout. CPU-bound, so it runs off the event loop."""
    return await asyncio.to_thread(_score_sync, layout, frame, reference_usable_m2)


def original_version(db: Session, room_id: int) -> Version | None:
    return (
        db.query(Version)
        .filter(Version.room_id == room_id, Version.version_type == "ORIGINAL")
        .order_by(Version.version_no.asc())
        .first()
    )


async def room_frame(db: Session, room_id: int, load_json: LoadJson) -> dict[str, Any] | None:
    """The room's original scan, which fixes the room bounds for scoring."""
    original = original_version(db, room_id)
    if original is None or not original.s3_json_url:
        return None
    return await load_json(original.s3_json_url, "원본")


async def compute_ai_record(target: Version, frame: dict[str, Any], load_json: LoadJson) -> dict[str, Any]:
    """Score of the AI layout that visitors are measured against.

    The AI layout is its own space reference, so its space part is full; what
    matters is its total and the usable area visitors compete with.
    """
    layout = await load_json(target.s3_json_url, "AI 배치")
    first = await score_roomplan_layout(layout, frame, None)
    scored = await score_roomplan_layout(layout, frame, first["usable_area_m2"])
    return {
        "score_version": SCORE_VERSION,
        "total_score": scored["total_score"],
        "usable_area_m2": scored["usable_area_m2"],
        "breakdown": scored["breakdown"],
        "disqualified": scored["disqualified"],
    }
