"""add rooms.optimization_status, separate from save status"""

revision = "f1a2b3c4d5e6"
down_revision = "d4e5f6a7b8c9"
branch_labels = None
depends_on = None

from alembic import op
import sqlalchemy as sa


def upgrade() -> None:
    op.add_column(
        "rooms",
        sa.Column("optimization_status", sa.String(length=20), server_default="NONE", nullable=False),
    )
    op.execute(
        """
        UPDATE rooms SET optimization_status = 'COMPLETED'
        WHERE id IN (SELECT DISTINCT room_id FROM versions WHERE version_type = 'OPTIMIZED')
        """
    )


def downgrade() -> None:
    op.drop_column("rooms", "optimization_status")
