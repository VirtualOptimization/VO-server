"""add created_at and version_name to versions"""

revision = 'b61bd30d9fb8'
down_revision = 'd1a7d4aec64e'  # 최초 뼈대 파일 버전
branch_labels = None
depends_on = None

from alembic import op
import sqlalchemy as sa

def upgrade() -> None:
    # 1. created_at 컬럼 추가
    op.add_column('versions', sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False))
    # 2. version_name 컬럼 추가
    op.add_column('versions', sa.Column('version_name', sa.String(length=100), nullable=True))

def downgrade() -> None:
    op.drop_column('versions', 'version_name')
    op.drop_column('versions', 'created_at')