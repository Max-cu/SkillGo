"""Adopt the SkillGo v0.1.0 schema as the migration baseline.

Revision ID: 20260822_0001
Revises:
Create Date: 2026-08-22

The application validates or creates the v0.1 schema before stamping this
revision. Future schema changes must be expressed as explicit migrations.
"""

from typing import Sequence


revision: str = "20260822_0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    # The baseline represents pre-existing tables and must never drop them.
    pass
