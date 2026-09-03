"""Add model API format for native OCR providers.

Revision ID: 20260903_0006
Revises: 20260903_0005
Create Date: 2026-09-03
"""

from typing import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260903_0006"
down_revision: str | None = "20260903_0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _columns(table_name: str) -> set[str]:
    return {column["name"] for column in sa.inspect(op.get_bind()).get_columns(table_name)}


def upgrade() -> None:
    if "api_format" not in _columns("model_connection_configs"):
        op.add_column(
            "model_connection_configs",
            sa.Column(
                "api_format", sa.String(32), server_default="openai", nullable=False
            ),
        )


def downgrade() -> None:
    if "api_format" in _columns("model_connection_configs"):
        op.drop_column("model_connection_configs", "api_format")
