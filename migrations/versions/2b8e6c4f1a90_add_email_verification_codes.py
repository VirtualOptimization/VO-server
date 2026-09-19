"""add email verification codes"""

revision = "2b8e6c4f1a90"
down_revision = "9c3d2a1b7e44"
branch_labels = None
depends_on = None

from alembic import op
import sqlalchemy as sa


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("is_email_verified", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )

    op.create_table(
        "email_verification_codes",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("email", sa.String(length=255), nullable=False),
        sa.Column("code_hash", sa.String(length=255), nullable=False),
        sa.Column("purpose", sa.String(length=30), server_default="SIGNUP", nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "idx_email_verification_codes_user_id",
        "email_verification_codes",
        ["user_id"],
        unique=False,
    )
    op.create_index(
        "idx_email_verification_codes_email",
        "email_verification_codes",
        ["email"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("idx_email_verification_codes_email", table_name="email_verification_codes")
    op.drop_index("idx_email_verification_codes_user_id", table_name="email_verification_codes")
    op.drop_table("email_verification_codes")
    op.drop_column("users", "is_email_verified")
