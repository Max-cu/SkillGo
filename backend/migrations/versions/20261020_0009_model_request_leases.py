"""Shared model-request leases for parallel page workers."""
from alembic import op
import sqlalchemy as sa


revision = "20261020_0009"
down_revision = "20260911_0008"
branch_labels = None
depends_on = None


def upgrade():
    tables = sa.inspect(op.get_bind()).get_table_names()
    if "model_request_gates" not in tables:
        op.create_table(
            "model_request_gates",
            sa.Column("pool", sa.String(64), primary_key=True),
        )
    if "model_request_leases" not in tables:
        op.create_table(
            "model_request_leases",
            sa.Column("token", sa.String(36), primary_key=True),
            sa.Column("pool", sa.String(64), nullable=False),
            sa.Column("job_id", sa.String(64), nullable=False),
            sa.Column("holder", sa.String(160), nullable=False),
            sa.Column("expires_at", sa.Integer(), nullable=False),
            sa.Column("created_at", sa.Integer(), nullable=False),
        )
        op.create_index(
            "ix_model_request_leases_pool_expires",
            "model_request_leases",
            ["pool", "expires_at"],
        )


def downgrade():
    op.drop_index("ix_model_request_leases_pool_expires", table_name="model_request_leases")
    op.drop_table("model_request_leases")
    op.drop_table("model_request_gates")
