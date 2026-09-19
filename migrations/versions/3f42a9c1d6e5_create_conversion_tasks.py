"""create conversion tasks"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "3f42a9c1d6e5"
down_revision = "8d3e0f1a2b4c"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "conversion_tasks",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("task_type", sa.String(length=50), nullable=False),
        sa.Column("status", sa.String(length=20), server_default="PENDING", nullable=False),
        sa.Column("source_key", sa.Text(), nullable=True),
        sa.Column("texture_key", sa.Text(), nullable=True),
        sa.Column("output_glb_key", sa.Text(), nullable=True),
        sa.Column("output_usdz_key", sa.Text(), nullable=True),
        sa.Column("furniture_model_id", sa.Integer(), nullable=True),
        sa.Column("version_id", sa.Integer(), nullable=True),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["furniture_model_id"], ["furniture_models.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["version_id"], ["versions.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_conversion_tasks_status", "conversion_tasks", ["status"], unique=False)
    op.create_index("idx_conversion_tasks_task_type", "conversion_tasks", ["task_type"], unique=False)
    op.create_index(
        "idx_conversion_tasks_furniture_model_id",
        "conversion_tasks",
        ["furniture_model_id"],
        unique=False,
    )
    op.create_index("idx_conversion_tasks_created_at", "conversion_tasks", ["created_at"], unique=False)


def downgrade() -> None:
    op.drop_index("idx_conversion_tasks_created_at", table_name="conversion_tasks")
    op.drop_index("idx_conversion_tasks_furniture_model_id", table_name="conversion_tasks")
    op.drop_index("idx_conversion_tasks_task_type", table_name="conversion_tasks")
    op.drop_index("idx_conversion_tasks_status", table_name="conversion_tasks")
    op.drop_table("conversion_tasks")
