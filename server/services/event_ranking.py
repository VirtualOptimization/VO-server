"""Exhibition leaderboard order, shared by the API and the winners export script."""

from __future__ import annotations

from sqlalchemy import func
from sqlalchemy.orm import Query, Session, joinedload

from shared.models.event import EventAttempt


def best_attempts(db: Session, event_id: int) -> Query:
    """Each participant's best valid attempt, in leaderboard order:
    score, then usable area, then elapsed time, then submission time."""
    order = (
        EventAttempt.total_score.desc(),
        EventAttempt.usable_area_m2.desc(),
        EventAttempt.elapsed_seconds.asc().nulls_last(),
        EventAttempt.created_at.asc(),
    )
    ranked = (
        db.query(
            EventAttempt.id.label("attempt_id"),
            func.row_number().over(partition_by=EventAttempt.participant_id, order_by=order).label("nth"),
        )
        .filter(EventAttempt.event_id == event_id, EventAttempt.disqualified.is_(False))
        .subquery()
    )
    return (
        db.query(EventAttempt)
        .join(ranked, ranked.c.attempt_id == EventAttempt.id)
        .options(joinedload(EventAttempt.participant))
        .filter(ranked.c.nth == 1)
        .order_by(*order)
    )


def participant_rank(db: Session, event_id: int, participant_id: int) -> tuple[int | None, int]:
    """(rank of the participant's best attempt, number of ranked participants)."""
    rows = best_attempts(db, event_id).all()
    for position, attempt in enumerate(rows, start=1):
        if attempt.participant_id == participant_id:
            return position, len(rows)
    return None, len(rows)
