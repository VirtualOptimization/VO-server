"""drop usdc_url from furniture_models"""

from alembic import op
import sqlalchemy as sa


revision = "e4a1c8d9f2b3"
down_revision = "c6f4a9d2b8e7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_column("furniture_models", "usdc_url")


def downgrade() -> None:
    op.add_column("furniture_models", sa.Column("usdc_url", sa.Text(), nullable=True))
