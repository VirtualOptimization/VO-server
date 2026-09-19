"""add users and furniture ownership"""

revision = "9c3d2a1b7e44"
down_revision = "a7f3d2c9b8e1"
branch_labels = None
depends_on = None

from alembic import op
import sqlalchemy as sa


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("login_id", sa.String(length=100), nullable=False),
        sa.Column("password_hash", sa.String(length=255), nullable=False),
        sa.Column("email", sa.String(length=255), nullable=True),
        sa.Column("nickname", sa.String(length=100), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("login_id"),
        sa.UniqueConstraint("email"),
    )

    op.add_column("rooms", sa.Column("user_id", sa.Integer(), nullable=True))
    op.create_foreign_key(
        "fk_rooms_user_id_users",
        "rooms",
        "users",
        ["user_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("idx_rooms_user_id", "rooms", ["user_id"], unique=False)

    op.add_column("furniture_models", sa.Column("user_id", sa.Integer(), nullable=True))
    op.add_column(
        "furniture_models",
        sa.Column("status", sa.String(length=20), server_default="READY", nullable=False),
    )
    op.add_column(
        "furniture_models",
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )
    op.add_column(
        "furniture_models",
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )
    op.create_foreign_key(
        "fk_furniture_models_user_id_users",
        "furniture_models",
        "users",
        ["user_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("idx_furniture_models_user_id", "furniture_models", ["user_id"], unique=False)
    op.create_index("idx_furniture_models_status", "furniture_models", ["status"], unique=False)


def downgrade() -> None:
    op.drop_index("idx_furniture_models_status", table_name="furniture_models")
    op.drop_index("idx_furniture_models_user_id", table_name="furniture_models")
    op.drop_constraint("fk_furniture_models_user_id_users", "furniture_models", type_="foreignkey")
    op.drop_column("furniture_models", "updated_at")
    op.drop_column("furniture_models", "created_at")
    op.drop_column("furniture_models", "status")
    op.drop_column("furniture_models", "user_id")

    op.drop_index("idx_rooms_user_id", table_name="rooms")
    op.drop_constraint("fk_rooms_user_id_users", "rooms", type_="foreignkey")
    op.drop_column("rooms", "user_id")

    op.drop_table("users")
