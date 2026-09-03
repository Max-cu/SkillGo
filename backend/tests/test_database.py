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
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "20260903_0006"


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
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "20260903_0006"


def test_v010_database_receives_storage_lifecycle_migration_without_data_loss():
    target = create_engine("sqlite://")
    Base.metadata.create_all(target)
    migrated_columns = {
        "agent_message_files": ("ix_agent_message_files_purged_at", "purged_at"),
        "workspace_files": ("ix_workspace_files_purged_at", "purged_at"),
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
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "20260903_0006"


def test_v022_database_receives_network_policy_columns_with_safe_defaults():
    target = create_engine("sqlite://")
    Base.metadata.create_all(target)
    with target.begin() as connection:
        connection.execute(text('ALTER TABLE "skill_versions" DROP COLUMN "network_enabled"'))
        connection.execute(text('ALTER TABLE "workflow_jobs" DROP COLUMN "network_enabled"'))
        connection.execute(text('ALTER TABLE "workflow_jobs" DROP COLUMN "network_enabled_by"'))
        connection.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)"))
        connection.execute(text("INSERT INTO alembic_version VALUES ('20260822_0003')"))
        connection.execute(
            text(
                "INSERT INTO users "
                "(id, email, display_name, password_hash, role, is_active, created_at, updated_at) "
                "VALUES ('u1', 'v022@example.com', 'v022', 'hash', 'USER', 1, "
                "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO skills "
                "(id, owner_id, slug, name, summary, description, category, visibility, icon, created_at, updated_at) "
                "VALUES ('s1', 'u1', 'v022-skill', 'v022 Skill', 'Existing v0.2.2 Skill version', '', "
                "'other', 'PRIVATE', 'sparkles', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO skill_versions "
                "(id, skill_id, created_by_id, version, status, skill_type, package_sha256, package_path, "
                "manifest, skill_md, input_schema, output_schema, requested_permissions, created_at, updated_at) "
                "VALUES ('v1', 's1', 'u1', '0.2.2', 'PUBLISHED', 'INSTRUCTION', "
                "'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', 'package.zip', "
                "'{}', '# Existing', '{}', '{}', '{}', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            )
        )

    initialize_schema(target)

    inspector = inspect(target)
    assert "network_enabled" in {
        column["name"] for column in inspector.get_columns("skill_versions")
    }
    assert {"network_enabled", "network_enabled_by"}.issubset(
        {column["name"] for column in inspector.get_columns("workflow_jobs")}
    )
    with target.connect() as connection:
        assert connection.scalar(
            text("SELECT network_enabled FROM skill_versions WHERE id='v1'")
        ) == 0
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "20260903_0006"


def test_v024_database_receives_model_api_format_with_safe_default():
    target = create_engine("sqlite://")
    Base.metadata.create_all(target)
    with target.begin() as connection:
        connection.execute(text('ALTER TABLE "model_connection_configs" DROP COLUMN "api_format"'))
        connection.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)"))
        connection.execute(text("INSERT INTO alembic_version VALUES ('20260903_0005')"))

    initialize_schema(target)

    assert "api_format" in {
        column["name"] for column in inspect(target).get_columns("model_connection_configs")
    }
    with target.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "20260903_0006"


def test_v023_database_receives_attachment_intelligence_columns_safely():
    target = create_engine("sqlite://")
    Base.metadata.create_all(target)
    attachment_columns = (
        "analysis_mode",
        "analysis_status",
        "analysis_model",
        "ocr_model",
        "analysis_error",
    )
    with target.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO model_connection_configs "
                "(id, model_name, base_url, api_key, timeout_seconds, temperature_milli, "
                "json_mode, native_tools, tls_verify, capabilities, is_default, "
                "is_default_vision, is_default_ocr, enabled, created_at, updated_at) "
                "VALUES ('m1', 'legacy-chat', 'https://model.example.com/v1', NULL, 120, "
                "200, 1, 1, 1, '[\"chat\"]', 1, 0, 0, 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            )
        )
        connection.execute(text('ALTER TABLE "model_connection_configs" DROP COLUMN "capabilities"'))
        connection.execute(text('ALTER TABLE "model_connection_configs" DROP COLUMN "is_default_vision"'))
        connection.execute(text('ALTER TABLE "model_connection_configs" DROP COLUMN "is_default_ocr"'))
        connection.execute(text('ALTER TABLE "workflow_jobs" DROP COLUMN "attachment_analysis_mode"'))
        for table_name in ("agent_message_files", "job_input_files"):
            for column_name in attachment_columns:
                connection.execute(
                    text(f'ALTER TABLE "{table_name}" DROP COLUMN "{column_name}"')
                )
        connection.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)"))
        connection.execute(text("INSERT INTO alembic_version VALUES ('20260826_0004')"))

    initialize_schema(target)

    inspector = inspect(target)
    assert {"capabilities", "is_default_vision", "is_default_ocr"}.issubset(
        {column["name"] for column in inspector.get_columns("model_connection_configs")}
    )
    assert "attachment_analysis_mode" in {
        column["name"] for column in inspector.get_columns("workflow_jobs")
    }
    for table_name in ("agent_message_files", "job_input_files"):
        assert set(attachment_columns).issubset(
            {column["name"] for column in inspector.get_columns(table_name)}
        )
    with target.connect() as connection:
        assert connection.scalar(
            text("SELECT capabilities FROM model_connection_configs WHERE id='m1'")
        ) == '["chat"]'
        assert connection.scalar(
            text("SELECT is_default_vision FROM model_connection_configs WHERE id='m1'")
        ) == 0
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "20260903_0006"


def test_incomplete_legacy_table_is_not_falsely_stamped():
    target = create_engine("sqlite://")
    with target.begin() as connection:
        connection.execute(text("CREATE TABLE users (id VARCHAR(36) PRIMARY KEY)"))

    with pytest.raises(SchemaMigrationError, match="Refusing to stamp"):
        initialize_schema(target)

    assert "alembic_version" not in inspect(target).get_table_names()
