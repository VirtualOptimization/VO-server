"""add mood to website guestbook entries"""

revision = "d5f1a8c3e2b7"
down_revision = "c4e9f2a7b1d3"
branch_labels = None
depends_on = None

from alembic import op
import sqlalchemy as sa


def upgrade() -> None:
    op.add_column("guestbook_entries", sa.Column("mood", sa.String(length=20), nullable=True))


def downgrade() -> None:
    op.drop_column("guestbook_entries", "mood")
