"""Add managed file retention state.

Revision ID: 20260822_0002
Revises: 20260822_0001
Create Date: 2026-08-22
"""

from typing import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260822_0002"
down_revision: str | None = "20260822_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _columns(table_name: str) -> set[str]:
    return {column["name"] for column in sa.inspect(op.get_bind()).get_columns(table_name)}


def _indexes(table_name: str) -> set[str]:
    return {index["name"] for index in sa.inspect(op.get_bind()).get_indexes(table_name)}


def _add_column(table_name: str, column: sa.Column) -> None:
    if column.name not in _columns(table_name):
        op.add_column(table_name, column)


def _add_index(name: str, table_name: str, columns: list[str]) -> None:
    if name not in _indexes(table_name):
        op.create_index(name, table_name, columns)


def upgrade() -> None:
    _add_column("agent_message_files", sa.Column("purged_at", sa.DateTime(timezone=True), nullable=True))
    _add_index("ix_agent_message_files_purged_at", "agent_message_files", ["purged_at"])
    _add_column("workspace_files", sa.Column("purged_at", sa.DateTime(timezone=True), nullable=True))
    _add_index("ix_workspace_files_purged_at", "workspace_files", ["purged_at"])
    _add_column(
        "workflow_jobs",
        sa.Column("storage_pinned", sa.Boolean(), server_default=sa.false(), nullable=False),
    )
    _add_index("ix_workflow_jobs_storage_pinned", "workflow_jobs", ["storage_pinned"])
    _add_column("job_input_files", sa.Column("purged_at", sa.DateTime(timezone=True), nullable=True))
    _add_index("ix_job_input_files_purged_at", "job_input_files", ["purged_at"])
    _add_column("artifacts", sa.Column("purged_at", sa.DateTime(timezone=True), nullable=True))
    _add_index("ix_artifacts_purged_at", "artifacts", ["purged_at"])


def downgrade() -> None:
    op.drop_index("ix_artifacts_purged_at", table_name="artifacts")
    op.drop_column("artifacts", "purged_at")
    op.drop_index("ix_job_input_files_purged_at", table_name="job_input_files")
    op.drop_column("job_input_files", "purged_at")
    op.drop_index("ix_workflow_jobs_storage_pinned", table_name="workflow_jobs")
    op.drop_column("workflow_jobs", "storage_pinned")
    op.drop_index("ix_workspace_files_purged_at", table_name="workspace_files")
    op.drop_column("workspace_files", "purged_at")
    op.drop_index("ix_agent_message_files_purged_at", table_name="agent_message_files")
    op.drop_column("agent_message_files", "purged_at")
