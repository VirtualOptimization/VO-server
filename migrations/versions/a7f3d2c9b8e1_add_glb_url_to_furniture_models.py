"""add glb_url to furniture_models"""

revision = "a7f3d2c9b8e1"
down_revision = "f8b8c14926ca"
branch_labels = None
depends_on = None

from alembic import op
import sqlalchemy as sa


def upgrade() -> None:
    op.add_column("furniture_models", sa.Column("glb_url", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("furniture_models", "glb_url")
