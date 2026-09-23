import importlib.util
from pathlib import Path
from unittest.mock import Mock

import pytest
from sqlalchemy import Column, Integer, MetaData, Table, create_engine, inspect

from app.database import Base


spec = importlib.util.spec_from_file_location(
    "legacy_migration", Path(__file__).resolve().parents[2] / "scripts/migrate_sqlite_to_postgres.py"
)
migration = importlib.util.module_from_spec(spec)
spec.loader.exec_module(migration)


@pytest.mark.parametrize("populated", [False, True])
def test_unsupported_tables_block_before_storage_or_target_writes(monkeypatch, populated):
    source = create_engine("sqlite://")
    target = create_engine("sqlite://")
    metadata = MetaData()
    extra = Table("future_records", metadata, Column("id", Integer, primary_key=True))
    metadata.create_all(source)
    Base.metadata.create_all(source)
    Base.metadata.create_all(target)
    copy_storage = Mock(return_value=({}, 0))
    monkeypatch.setattr(migration, "copy_storage", copy_storage)
    try:
        if populated:
            with source.begin() as connection:
                connection.execute(extra.insert().values(id=1))
            assert migration.unsupported_populated_tables(source) == ["future_records"]
            before = migration.table_counts(target)
            with pytest.raises(RuntimeError, match="future_records.*No files or target data were changed"):
                migration.migrate(source, target, Path("source"), Path("target"))
            copy_storage.assert_not_called()
            assert "future_records" not in inspect(target).get_table_names()
            assert migration.table_counts(target) == before
        else:
            assert migration.unsupported_populated_tables(source) == []
            report = migration.migrate(source, target, Path("source"), Path("target"))
            copy_storage.assert_called_once()
            assert report["inserted"] == {}
    finally:
        source.dispose()
        target.dispose()
