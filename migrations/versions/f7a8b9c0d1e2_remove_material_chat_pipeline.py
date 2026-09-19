"""remove material chatbot

Revision ID: f7a8b9c0d1e2
Revises: e1f2a3b4c5d6
"""

from alembic import op
import sqlalchemy as sa


revision = "f7a8b9c0d1e2"
down_revision = "e1f2a3b4c5d6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_index("idx_material_chat_jobs_status", table_name="material_chat_jobs")
    op.drop_index("idx_material_chat_jobs_user_created", table_name="material_chat_jobs")
    op.drop_table("material_chat_jobs")


def downgrade() -> None:
    op.create_table(
        "material_chat_jobs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("model_id", sa.Integer(), nullable=False),
        sa.Column("texture_preset_id", sa.Integer(), nullable=True),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("assistant_message", sa.Text(), nullable=True),
        sa.Column("material_type", sa.String(length=50), nullable=True),
        sa.Column("material_name", sa.String(length=100), nullable=True),
        sa.Column("image_prompt", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=20), server_default="PENDING", nullable=False),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["model_id"], ["furniture_models.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["texture_preset_id"], ["texture_presets.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "idx_material_chat_jobs_user_created",
        "material_chat_jobs",
        ["user_id", "created_at"],
        unique=False,
    )
    op.create_index("idx_material_chat_jobs_status", "material_chat_jobs", ["status"], unique=False)
