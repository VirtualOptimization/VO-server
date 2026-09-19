"""remove texture features

Revision ID: b9c0d1e2f3a4
Revises: a8b9c0d1e2f3
"""

from alembic import op
import sqlalchemy as sa


revision = "b9c0d1e2f3a4"
down_revision = "a8b9c0d1e2f3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        DELETE FROM conversion_tasks
        WHERE task_type IN ('MATERIAL_ASSET', 'BASE_MATERIAL_ASSET')
        """
    )
    op.execute(
        """
        UPDATE versions
        SET json_data = json_data - 'material_assets'
        WHERE json_data IS NOT NULL
          AND json_data -> 'material_assets' IS NOT NULL
        """
    )
    op.drop_table("furniture_material_assets")
    op.drop_table("texture_presets")
    op.drop_column("conversion_tasks", "texture_key")


def downgrade() -> None:
    op.add_column("conversion_tasks", sa.Column("texture_key", sa.Text(), nullable=True))

    op.create_table(
        "texture_presets",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("preset_key", sa.String(length=100), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("texture_s3_key", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("source", sa.String(length=20), server_default="PRESET", nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_texture_presets_user_id_users",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("preset_key", name="uq_texture_presets_preset_key"),
    )
    op.create_index("idx_texture_presets_preset_key", "texture_presets", ["preset_key"], unique=False)
    op.create_index("idx_texture_presets_user_id", "texture_presets", ["user_id"], unique=False)

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
