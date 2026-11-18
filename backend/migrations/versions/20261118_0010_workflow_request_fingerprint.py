"""Store request fingerprints for asynchronous workflow idempotency."""

import sqlalchemy as sa
from alembic import op

revision = "20261118_0010"
down_revision = "20261022_0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    columns = sa.inspect(op.get_bind()).get_columns("workflow_endpoint_requests")
    if not any(column["name"] == "request_fingerprint" for column in columns):
        op.add_column(
            "workflow_endpoint_requests",
            sa.Column("request_fingerprint", sa.String(64), nullable=True),
        )


def downgrade() -> None:
    op.drop_column("workflow_endpoint_requests", "request_fingerprint")
