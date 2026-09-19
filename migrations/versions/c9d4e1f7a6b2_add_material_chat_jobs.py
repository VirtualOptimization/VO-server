"""add material chatbot jobs and generated texture ownership

Revision ID: c9d4e1f7a6b2
Revises: ab83d91e6c74
"""

from alembic import op
import sqlalchemy as sa


revision = "c9d4e1f7a6b2"
down_revision = "ab83d91e6c74"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("texture_presets", sa.Column("user_id", sa.Integer(), nullable=True))
    op.add_column(
        "texture_presets",
        sa.Column("source", sa.String(length=20), server_default="PRESET", nullable=False),
    )
    op.create_foreign_key(
        "fk_texture_presets_user_id_users",
        "texture_presets",
        "users",
        ["user_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_index("idx_texture_presets_user_id", "texture_presets", ["user_id"], unique=False)

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


def downgrade() -> None:
    op.drop_index("idx_material_chat_jobs_status", table_name="material_chat_jobs")
    op.drop_index("idx_material_chat_jobs_user_created", table_name="material_chat_jobs")
    op.drop_table("material_chat_jobs")
    op.drop_index("idx_texture_presets_user_id", table_name="texture_presets")
    op.drop_constraint("fk_texture_presets_user_id_users", "texture_presets", type_="foreignkey")
    op.drop_column("texture_presets", "source")
    op.drop_column("texture_presets", "user_id")
