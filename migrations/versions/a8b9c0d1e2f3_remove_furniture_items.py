"""remove unused furniture items table

Revision ID: a8b9c0d1e2f3
Revises: f7a8b9c0d1e2
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "a8b9c0d1e2f3"
down_revision = "f7a8b9c0d1e2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_table("furniture_items")


def downgrade() -> None:
    op.create_table(
        "furniture_items",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("version_id", sa.Integer(), nullable=False),
        sa.Column("model_id", sa.Integer(), nullable=True),
        sa.Column("item_key", sa.String(length=100), nullable=False),
        sa.Column("pos_x", sa.Float(), nullable=False),
        sa.Column("pos_y", sa.Float(), nullable=False),
        sa.Column("pos_z", sa.Float(), nullable=False),
        sa.Column("rot_x", sa.Float(), nullable=False),
        sa.Column("rot_y", sa.Float(), nullable=False),
        sa.Column("rot_z", sa.Float(), nullable=False),
        sa.Column("scale_x", sa.Float(), server_default="1.0", nullable=False),
        sa.Column("scale_y", sa.Float(), server_default="1.0", nullable=False),
        sa.Column("scale_z", sa.Float(), server_default="1.0", nullable=False),
        sa.Column("footprint_polygon", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.ForeignKeyConstraint(["model_id"], ["furniture_models.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["version_id"], ["versions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
