"""make email verification user nullable"""

from alembic import op
import sqlalchemy as sa


revision = "7d2b91e8c4f0"
down_revision = "2b8e6c4f1a90"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "email_verification_codes",
        "user_id",
        existing_type=sa.Integer(),
        nullable=True,
    )


def downgrade() -> None:
    op.alter_column(
        "email_verification_codes",
        "user_id",
        existing_type=sa.Integer(),
        nullable=False,
    )
