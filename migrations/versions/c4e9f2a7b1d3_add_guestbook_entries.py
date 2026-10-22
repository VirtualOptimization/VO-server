"""add website guestbook entries"""

revision = "c4e9f2a7b1d3"
down_revision = "b3d8e1f4a6c2"
branch_labels = None
depends_on = None

from alembic import op
import sqlalchemy as sa


def upgrade() -> None:
    op.create_table(
        "guestbook_entries",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("nickname", sa.String(length=20), nullable=False),
        sa.Column("message", sa.String(length=200), nullable=False),
        sa.Column("photo_key", sa.Text(), nullable=True),
        sa.Column("consent_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_guestbook_entries_created_at", "guestbook_entries", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_guestbook_entries_created_at", table_name="guestbook_entries")
    op.drop_table("guestbook_entries")
