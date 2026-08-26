"""Add administrator-controlled Skill network access.

Revision ID: 20260826_0004
Revises: 20260822_0003
Create Date: 2026-08-26
"""

from typing import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260826_0004"
down_revision: str | None = "20260822_0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _columns(table_name: str) -> set[str]:
    return {column["name"] for column in sa.inspect(op.get_bind()).get_columns(table_name)}


def _add_column(table_name: str, column: sa.Column) -> None:
    if column.name not in _columns(table_name):
        op.add_column(table_name, column)


def upgrade() -> None:
    _add_column(
        "skill_versions",
        sa.Column("network_enabled", sa.Boolean(), server_default=sa.false(), nullable=False),
    )
    _add_column(
        "workflow_jobs",
        sa.Column("network_enabled", sa.Boolean(), server_default=sa.false(), nullable=False),
    )
    _add_column(
        "workflow_jobs",
        sa.Column("network_enabled_by", sa.JSON(), server_default="[]", nullable=False),
    )


def downgrade() -> None:
    workflow_columns = _columns("workflow_jobs")
    if "network_enabled_by" in workflow_columns:
        op.drop_column("workflow_jobs", "network_enabled_by")
    if "network_enabled" in workflow_columns:
        op.drop_column("workflow_jobs", "network_enabled")
    if "network_enabled" in _columns("skill_versions"):
        op.drop_column("skill_versions", "network_enabled")
