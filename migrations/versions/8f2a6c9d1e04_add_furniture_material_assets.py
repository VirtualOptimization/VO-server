"""add furniture material assets

Revision ID: 8f2a6c9d1e04
Revises: 5c1d9e8f7a32
Create Date: 2026-08-09 09:30:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "8f2a6c9d1e04"
down_revision = "5c1d9e8f7a32"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "furniture_material_assets",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("base_model_id", sa.Integer(), nullable=False),
        sa.Column("room_id", sa.Integer(), nullable=False),
        sa.Column("version_id", sa.Integer(), nullable=False),
        sa.Column("furniture_instance_id", sa.String(length=100), nullable=False),
        sa.Column("material_preset_id", sa.String(length=100), nullable=False),
        sa.Column("material_name", sa.String(length=100), nullable=True),
        sa.Column("glb_url", sa.Text(), nullable=True),
        sa.Column("usdz_url", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=20), server_default="UPLOADING", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["base_model_id"], ["furniture_models.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["room_id"], ["rooms.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["version_id"], ["versions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "user_id",
            "room_id",
            "version_id",
            "furniture_instance_id",
            "base_model_id",
            name="uq_furniture_material_asset",
        ),
    )
    op.create_index(
        "idx_furniture_material_assets_base_model_id",
        "furniture_material_assets",
        ["base_model_id"],
    )
    op.create_index(
        "idx_furniture_material_assets_status",
        "furniture_material_assets",
        ["status"],
    )
    op.create_index(
        "idx_furniture_material_assets_user_id",
        "furniture_material_assets",
        ["user_id"],
    )


def downgrade() -> None:
    op.drop_index("idx_furniture_material_assets_user_id", table_name="furniture_material_assets")
    op.drop_index("idx_furniture_material_assets_status", table_name="furniture_material_assets")
    op.drop_index("idx_furniture_material_assets_base_model_id", table_name="furniture_material_assets")
    op.drop_table("furniture_material_assets")
