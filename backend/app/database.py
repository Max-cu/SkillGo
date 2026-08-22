from __future__ import annotations

from collections.abc import Generator
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Connection, Engine, make_url
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import settings


class Base(DeclarativeBase):
    pass


def ensure_sqlite_parent(database_url: str) -> None:
    url = make_url(database_url)
    if not url.drivername.startswith("sqlite") or not url.database:
        return
    if url.database == ":memory:":
        return
    Path(url.database).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)


ensure_sqlite_parent(settings.database_url)
connect_args = (
    {"check_same_thread": False}
    if settings.database_url.startswith("sqlite")
    else {}
)
engine = create_engine(settings.database_url, connect_args=connect_args, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


class SchemaMigrationError(RuntimeError):
    """Raised when an unversioned database cannot be adopted safely."""


BASELINE_REVISION = "20260822_0001"
POST_BASELINE_COLUMNS = {
    "agent_message_files": {"purged_at"},
    "workspace_files": {"purged_at"},
    "job_input_files": {"purged_at"},
    "artifacts": {"purged_at"},
}


def _migration_config(connection: Connection) -> Config:
    backend_root = Path(__file__).resolve().parents[1]
    config = Config(str(backend_root / "alembic.ini"))
    config.set_main_option("script_location", str(backend_root / "migrations"))
    config.attributes["connection"] = connection
    return config


def _missing_columns(
    connection: Connection,
    table_names: set[str],
    *,
    ignore: dict[str, set[str]] | None = None,
) -> dict[str, list[str]]:
    inspector = inspect(connection)
    missing: dict[str, list[str]] = {}
    for table_name in sorted(table_names & set(Base.metadata.tables)):
        expected = set(Base.metadata.tables[table_name].columns.keys())
        actual = {column["name"] for column in inspector.get_columns(table_name)}
        if absent := sorted(expected - actual - (ignore or {}).get(table_name, set())):
            missing[table_name] = absent
    return missing


def _validate_schema(connection: Connection) -> None:
    inspector = inspect(connection)
    existing_tables = set(inspector.get_table_names())
    expected_tables = set(Base.metadata.tables)
    missing_tables = sorted(expected_tables - existing_tables)
    missing_columns = _missing_columns(connection, expected_tables)
    if missing_tables or missing_columns:
        details: list[str] = []
        if missing_tables:
            details.append("missing tables: " + ", ".join(missing_tables))
        if missing_columns:
            details.append(
                "missing columns: "
                + "; ".join(
                    f"{table}({', '.join(columns)})"
                    for table, columns in missing_columns.items()
                )
            )
        raise SchemaMigrationError("Database schema validation failed: " + " | ".join(details))


def initialize_schema(target_engine: Engine | None = None) -> None:
    """Adopt the v0.1 schema once, then apply versioned Alembic migrations.

    Existing v0.1 installations have no ``alembic_version`` table. They are
    stamped only after every existing table is checked for required columns and
    any entirely missing additive tables are created. A partially modified
    table is rejected instead of being stamped as current.
    """

    # Importing models here guarantees all tables are registered when this
    # helper is used by maintenance commands outside the FastAPI import path.
    from . import models as _models  # noqa: F401

    active_engine = target_engine or engine

    with active_engine.begin() as connection:
        if connection.dialect.name == "postgresql":
            connection.execute(text("SELECT pg_advisory_xact_lock(73455001)"))

        inspector = inspect(connection)
        existing_tables = set(inspector.get_table_names())
        business_tables = existing_tables - {"alembic_version"}
        has_version_table = "alembic_version" in existing_tables

        if not business_tables:
            Base.metadata.create_all(bind=connection)
            config = _migration_config(connection)
            command.stamp(config, "head")
        elif not has_version_table:
            missing_columns = _missing_columns(
                connection,
                business_tables,
                ignore=POST_BASELINE_COLUMNS,
            )
            if missing_columns:
                formatted = "; ".join(
                    f"{table}({', '.join(columns)})"
                    for table, columns in missing_columns.items()
                )
                raise SchemaMigrationError(
                    "Refusing to stamp an incomplete legacy database; "
                    f"missing columns: {formatted}"
                )
            # v0.1 evolved by adding whole tables. Creating only absent tables
            # preserves populated tables before the baseline is stamped.
            Base.metadata.create_all(bind=connection)
            config = _migration_config(connection)
            command.stamp(config, BASELINE_REVISION)
            command.upgrade(config, "head")
        else:
            config = _migration_config(connection)
            command.upgrade(config, "head")
        _validate_schema(connection)


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
