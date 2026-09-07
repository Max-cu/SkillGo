"""Durable task clarification and per-model agent budgets."""
from alembic import op
import sqlalchemy as sa

revision = '20260907_0007'
down_revision = '20260903_0006'
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if 'workflow_job_memory' not in inspector.get_table_names():
        op.create_table('workflow_job_memory',
                        sa.Column('job_id', sa.String(36), sa.ForeignKey('workflow_jobs.id', ondelete='CASCADE'), primary_key=True),
                        sa.Column('data', sa.JSON(), nullable=False))
    if 'agent_options' not in {column['name'] for column in inspector.get_columns('model_connection_configs')}:
        op.add_column('model_connection_configs', sa.Column('agent_options', sa.JSON(), server_default='{}', nullable=False))
    # AgentRun used VARCHAR(9) for the former longest Enum value. The new
    # WAITING_USER value must fit on PostgreSQL as well as SQLite.
    if bind.dialect.name == 'postgresql':
        for table in ('agent_runs', 'runs'):
            op.alter_column(table, 'status', type_=sa.String(20), existing_nullable=False)


def downgrade():
    op.drop_table('workflow_job_memory')
    op.drop_column('model_connection_configs', 'agent_options')
