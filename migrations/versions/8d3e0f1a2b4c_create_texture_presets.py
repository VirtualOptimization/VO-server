"""create texture presets"""

from alembic import op
import sqlalchemy as sa


revision = "8d3e0f1a2b4c"
down_revision = "5c1d9e8f7a32"
branch_labels = None
depends_on = None


texture_presets = sa.table(
    "texture_presets",
    sa.column("preset_key", sa.String(length=100)),
    sa.column("name", sa.String(length=100)),
    sa.column("texture_s3_key", sa.Text()),
)


def upgrade() -> None:
    op.create_table(
        "texture_presets",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("preset_key", sa.String(length=100), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("texture_s3_key", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("preset_key", name="uq_texture_presets_preset_key"),
    )
    op.create_index("idx_texture_presets_preset_key", "texture_presets", ["preset_key"], unique=False)

    op.bulk_insert(
        texture_presets,
        [
            {
                "preset_key": "wood_01",
                "name": "나무질감",
                "texture_s3_key": "textures/wood_01.jpg",
            },
            {
                "preset_key": "rattan_01",
                "name": "라탄질감",
                "texture_s3_key": "textures/rattan_01.jpg",
            },
            {
                "preset_key": "marble_01",
                "name": "대리석질감",
                "texture_s3_key": "textures/marble_01.jpg",
            },
        ],
    )


def downgrade() -> None:
    op.drop_index("idx_texture_presets_preset_key", table_name="texture_presets")
    op.drop_table("texture_presets")
