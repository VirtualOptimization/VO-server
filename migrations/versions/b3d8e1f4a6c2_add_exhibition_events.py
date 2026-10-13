"""add exhibition events, participants and attempts"""

revision = "b3d8e1f4a6c2"
down_revision = "f1a2b3c4d5e6"
branch_labels = None
depends_on = None

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


def upgrade() -> None:
    op.create_table(
        "events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("owner_user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("room_id", sa.Integer(), sa.ForeignKey("rooms.id", ondelete="CASCADE"), nullable=False),
        sa.Column("start_version_id", sa.Integer(), sa.ForeignKey("versions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("target_version_id", sa.Integer(), sa.ForeignKey("versions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("max_attempts", sa.Integer(), server_default="1", nullable=False),
        sa.Column("ai_record", postgresql.JSONB(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_table(
        "event_participants",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("event_id", sa.Integer(), sa.ForeignKey("events.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(length=30), nullable=False),
        sa.Column("phone", sa.String(length=20), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False, unique=True),
        sa.Column("privacy_consent_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("event_id", "phone", name="uq_event_participants_event_phone"),
    )
    op.create_table(
        "event_attempts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("event_id", sa.Integer(), sa.ForeignKey("events.id", ondelete="CASCADE"), nullable=False),
        sa.Column(
            "participant_id",
            sa.Integer(),
            sa.ForeignKey("event_participants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("idempotency_key", sa.String(length=64), nullable=False),
        sa.Column("total_score", sa.Float(), nullable=False),
        sa.Column("disqualified", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("breakdown", postgresql.JSONB(), nullable=True),
        sa.Column("hints", postgresql.JSONB(), nullable=True),
        sa.Column("usable_area_m2", sa.Float(), nullable=False),
        sa.Column("beat_ai", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("elapsed_seconds", sa.Integer(), nullable=True),
        sa.Column("layout_s3_url", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("participant_id", "idempotency_key", name="uq_event_attempts_participant_key"),
    )
    op.create_index("idx_event_attempts_event_rank", "event_attempts", ["event_id", "disqualified", "total_score"])


def downgrade() -> None:
    op.drop_index("idx_event_attempts_event_rank", table_name="event_attempts")
    op.drop_table("event_attempts")
    op.drop_table("event_participants")
    op.drop_table("events")
