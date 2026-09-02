"""create reusable base furniture material assets

Revision ID: e1f2a3b4c5d6
Revises: c9d4e1f7a6b2
"""

from alembic import op
import sqlalchemy as sa


revision = "e1f2a3b4c5d6"
down_revision = "c9d4e1f7a6b2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "furniture_material_assets",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("furniture_model_id", sa.Integer(), nullable=False),
        sa.Column("texture_preset_id", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=20), server_default="PENDING", nullable=False),
        sa.Column("glb_url", sa.Text(), nullable=True),
        sa.Column("usdz_url", sa.Text(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["furniture_model_id"], ["furniture_models.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["texture_preset_id"], ["texture_presets.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "furniture_model_id",
            "texture_preset_id",
            name="uq_furniture_material_assets_model_preset",
        ),
    )
    op.create_index(
        "idx_furniture_material_assets_status",
        "furniture_material_assets",
        ["status"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("idx_furniture_material_assets_status", table_name="furniture_material_assets")
    op.drop_table("furniture_material_assets")
