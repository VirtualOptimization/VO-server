"""rename converted_fbx_url to converted_glb_url"""

revision = 'f8b8c14926ca'
down_revision = 'b61bd30d9fb8'
branch_labels = None
depends_on = None

from alembic import op
import sqlalchemy as sa



def upgrade() -> None:
    op.alter_column('versions', 'converted_fbx_url', new_column_name='converted_glb_url')


def downgrade() -> None:
    op.alter_column('versions', 'converted_glb_url', new_column_name='converted_fbx_url')
