"""add usdz_url to furniture_models"""

from alembic import op
import sqlalchemy as sa


revision = "4db7c2e6a1f9"
down_revision = "e4a1c8d9f2b3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("furniture_models", sa.Column("usdz_url", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("furniture_models", "usdz_url")
