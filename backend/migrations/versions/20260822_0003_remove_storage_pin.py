"""Remove optional task storage retention.

Revision ID: 20260822_0003
Revises: 20260822_0002
Create Date: 2026-08-22
"""

from typing import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260822_0003"
down_revision: str | None = "20260822_0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _columns() -> set[str]:
    return {column["name"] for column in sa.inspect(op.get_bind()).get_columns("workflow_jobs")}


def _indexes() -> set[str]:
    return {index["name"] for index in sa.inspect(op.get_bind()).get_indexes("workflow_jobs")}


def upgrade() -> None:
    if "ix_workflow_jobs_storage_pinned" in _indexes():
        op.drop_index("ix_workflow_jobs_storage_pinned", table_name="workflow_jobs")
    if "storage_pinned" in _columns():
        op.drop_column("workflow_jobs", "storage_pinned")


def downgrade() -> None:
    if "storage_pinned" not in _columns():
        op.add_column(
            "workflow_jobs",
            sa.Column("storage_pinned", sa.Boolean(), server_default=sa.false(), nullable=False),
        )
    if "ix_workflow_jobs_storage_pinned" not in _indexes():
        op.create_index("ix_workflow_jobs_storage_pinned", "workflow_jobs", ["storage_pinned"])
