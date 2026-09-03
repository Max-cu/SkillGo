"""Add model capabilities and attachment intelligence metadata.

Revision ID: 20260903_0005
Revises: 20260826_0004
Create Date: 2026-09-03
"""

from typing import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260903_0005"
down_revision: str | None = "20260826_0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _columns(table_name: str) -> set[str]:
    return {column["name"] for column in sa.inspect(op.get_bind()).get_columns(table_name)}


def _add_column(table_name: str, column: sa.Column) -> None:
    if column.name not in _columns(table_name):
        op.add_column(table_name, column)


def upgrade() -> None:
    _add_column(
        "model_connection_configs",
        sa.Column("capabilities", sa.JSON(), server_default='["chat"]', nullable=False),
    )
    _add_column(
        "model_connection_configs",
        sa.Column("is_default_vision", sa.Boolean(), server_default=sa.false(), nullable=False),
    )
    _add_column(
        "model_connection_configs",
        sa.Column("is_default_ocr", sa.Boolean(), server_default=sa.false(), nullable=False),
    )
    _add_column(
        "workflow_jobs",
        sa.Column("attachment_analysis_mode", sa.String(32), server_default="vision", nullable=False),
    )
    for table_name in ("agent_message_files", "job_input_files"):
        _add_column(table_name, sa.Column("analysis_mode", sa.String(32), nullable=True))
        _add_column(table_name, sa.Column("analysis_status", sa.String(32), nullable=True))
        _add_column(table_name, sa.Column("analysis_model", sa.String(160), nullable=True))
        _add_column(table_name, sa.Column("ocr_model", sa.String(160), nullable=True))
        _add_column(table_name, sa.Column("analysis_error", sa.String(1000), nullable=True))


def downgrade() -> None:
    for table_name in ("job_input_files", "agent_message_files"):
        columns = _columns(table_name)
        for column_name in (
            "analysis_error",
            "ocr_model",
            "analysis_model",
            "analysis_status",
            "analysis_mode",
        ):
            if column_name in columns:
                op.drop_column(table_name, column_name)
    if "attachment_analysis_mode" in _columns("workflow_jobs"):
        op.drop_column("workflow_jobs", "attachment_analysis_mode")
    model_columns = _columns("model_connection_configs")
    for column_name in ("is_default_ocr", "is_default_vision", "capabilities"):
        if column_name in model_columns:
            op.drop_column("model_connection_configs", column_name)
