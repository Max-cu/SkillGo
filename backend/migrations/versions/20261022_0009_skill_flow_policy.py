"""Administrator-controlled per-version Skill flow and per-job policy snapshot."""
from alembic import op
import sqlalchemy as sa
revision = '20261022_0009'
down_revision = '20260911_0008'
branch_labels = None
depends_on = None


def upgrade():
    for table in ('skill_versions', 'workflow_jobs'):
        if 'follow_skill_flow' not in {c['name'] for c in sa.inspect(op.get_bind()).get_columns(table)}:
            op.add_column(table, sa.Column('follow_skill_flow', sa.Boolean(), nullable=False, server_default=sa.false()))


def downgrade():
    for table in ('workflow_jobs', 'skill_versions'):
        op.drop_column(table, 'follow_skill_flow')
