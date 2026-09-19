"""add status to versions

Revision ID: ab83d91e6c74
Revises: 3f42a9c1d6e5
"""

from alembic import op
import sqlalchemy as sa


revision = "ab83d91e6c74"
down_revision = "3f42a9c1d6e5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "versions",
        sa.Column("status", sa.String(length=20), server_default="READY", nullable=False),
    )
    op.create_check_constraint(
        "chk_versions_status",
        "versions",
        "status IN ('PENDING', 'READY', 'FAILED')",
    )


def downgrade() -> None:
    op.drop_constraint("chk_versions_status", "versions", type_="check")
    op.drop_column("versions", "status")
