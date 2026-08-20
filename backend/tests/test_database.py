from __future__ import annotations

from uuid import uuid4

from app.database import ensure_sqlite_parent


def test_sqlite_parent_is_created(test_data_root):
    target = test_data_root / uuid4().hex / "nested" / "skillgo.db"
    ensure_sqlite_parent(f"sqlite:///{target.as_posix()}")
    assert target.parent.is_dir()
