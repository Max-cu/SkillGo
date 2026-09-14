"""Prepared environment cache and independent Skill version bindings."""
from alembic import op
import sqlalchemy as sa
revision = '20260911_0008'
down_revision = '20260907_0007'
branch_labels = None
depends_on = None


def upgrade():
    tables = sa.inspect(op.get_bind()).get_table_names()
    if 'prepared_environments' not in tables:
        op.create_table('prepared_environments',
        sa.Column('digest', sa.String(64), primary_key=True),
        sa.Column('status', sa.String(20), nullable=False),
        sa.Column('spec', sa.JSON(), nullable=False),
        sa.Column('image_id', sa.String(80)),
        sa.Column('inventory', sa.JSON(), nullable=False),
        sa.Column('error_message', sa.Text()),
        sa.Column('attempt', sa.String(36)),
        sa.Column('lease_until', sa.DateTime(timezone=True)),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False))
        op.create_index('ix_prepared_environments_status', 'prepared_environments', ['status'])
    if 'skill_environment_bindings' not in tables:
        op.create_table('skill_environment_bindings',
        sa.Column('version_id', sa.String(36), sa.ForeignKey('skill_versions.id', ondelete='CASCADE'), primary_key=True),
        sa.Column('environment_digest', sa.String(64), sa.ForeignKey('prepared_environments.digest'), nullable=False),
        sa.Column('analysis', sa.JSON(), nullable=False))
        op.create_index('ix_skill_environment_bindings_environment_digest', 'skill_environment_bindings', ['environment_digest'])


def downgrade():
    op.drop_table('skill_environment_bindings')
    op.drop_table('prepared_environments')
