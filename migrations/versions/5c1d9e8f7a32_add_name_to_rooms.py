"""add name to rooms"""

from alembic import op
import sqlalchemy as sa


revision = "5c1d9e8f7a32"
down_revision = "4db7c2e6a1f9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("rooms", sa.Column("name", sa.String(length=100), nullable=True))


def downgrade() -> None:
    op.drop_column("rooms", "name")
