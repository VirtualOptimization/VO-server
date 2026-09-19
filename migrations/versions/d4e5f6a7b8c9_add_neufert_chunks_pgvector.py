"""add neufert_chunks table with pgvector embedding column"""

revision = "d4e5f6a7b8c9"
down_revision = "b9c0d1e2f3a4"
branch_labels = None
depends_on = None

from alembic import op
import sqlalchemy as sa
from pgvector.sqlalchemy import Vector

BGE_M3_EMBEDDING_DIM = 1024


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.create_table(
        "neufert_chunks",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("source_name", sa.String(length=255), nullable=True),
        sa.Column("page_start", sa.Integer(), nullable=True),
        sa.Column("page_end", sa.Integer(), nullable=True),
        sa.Column("section_title", sa.String(length=255), nullable=True),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("embedding", Vector(BGE_M3_EMBEDDING_DIM), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "idx_neufert_chunks_content_hash", "neufert_chunks", ["content_hash"], unique=True
    )
    op.execute(
        "CREATE INDEX idx_neufert_chunks_embedding ON neufert_chunks "
        "USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64)"
    )


def downgrade() -> None:
    op.drop_index("idx_neufert_chunks_embedding", table_name="neufert_chunks")
    op.drop_index("idx_neufert_chunks_content_hash", table_name="neufert_chunks")
    op.drop_table("neufert_chunks")
