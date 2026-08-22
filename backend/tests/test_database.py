from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import create_engine, inspect, text

from app.database import Base, SchemaMigrationError, ensure_sqlite_parent, initialize_schema
from app.models import Favorite


def test_sqlite_parent_is_created(test_data_root):
    target = test_data_root / uuid4().hex / "nested" / "skillgo.db"
    ensure_sqlite_parent(f"sqlite:///{target.as_posix()}")
    assert target.parent.is_dir()


def test_blank_database_is_created_and_stamped_at_head():
    target = create_engine("sqlite://")

    initialize_schema(target)
    initialize_schema(target)

    tables = set(inspect(target).get_table_names())
    assert set(Base.metadata.tables).issubset(tables)
    assert "alembic_version" in tables
    with target.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "20260822_0002"


def test_complete_legacy_database_is_adopted_without_losing_rows():
    target = create_engine("sqlite://")
    Base.metadata.create_all(target)
    Favorite.__table__.drop(target)
    with target.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO users "
                "(id, email, display_name, password_hash, role, is_active, created_at, updated_at) "
                "VALUES ('u1', 'owner@example.com', 'Owner', 'hash', 'SUPER_ADMIN', 1, "
                "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            )
        )

    initialize_schema(target)

    assert "favorites" in inspect(target).get_table_names()
    with target.connect() as connection:
        assert connection.scalar(text("SELECT count(*) FROM users")) == 1
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "20260822_0002"


def test_v010_database_receives_storage_lifecycle_migration_without_data_loss():
    target = create_engine("sqlite://")
    Base.metadata.create_all(target)
    migrated_columns = {
        "agent_message_files": ("ix_agent_message_files_purged_at", "purged_at"),
        "workspace_files": ("ix_workspace_files_purged_at", "purged_at"),
        "workflow_jobs": ("ix_workflow_jobs_storage_pinned", "storage_pinned"),
        "job_input_files": ("ix_job_input_files_purged_at", "purged_at"),
        "artifacts": ("ix_artifacts_purged_at", "purged_at"),
    }
    with target.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO users "
                "(id, email, display_name, password_hash, role, is_active, created_at, updated_at) "
                "VALUES ('u1', 'legacy@example.com', 'Legacy', 'hash', 'USER', 1, "
                "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            )
        )
        for table_name, (index_name, column_name) in migrated_columns.items():
            connection.execute(text(f'DROP INDEX "{index_name}"'))
            connection.execute(text(f'ALTER TABLE "{table_name}" DROP COLUMN "{column_name}"'))

    initialize_schema(target)

    inspector = inspect(target)
    for table_name, (_, column_name) in migrated_columns.items():
        assert column_name in {column["name"] for column in inspector.get_columns(table_name)}
    with target.connect() as connection:
        assert connection.scalar(text("SELECT count(*) FROM users")) == 1
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "20260822_0002"


def test_incomplete_legacy_table_is_not_falsely_stamped():
    target = create_engine("sqlite://")
    with target.begin() as connection:
        connection.execute(text("CREATE TABLE users (id VARCHAR(36) PRIMARY KEY)"))

    with pytest.raises(SchemaMigrationError, match="Refusing to stamp"):
        initialize_schema(target)

    assert "alembic_version" not in inspect(target).get_table_names()
