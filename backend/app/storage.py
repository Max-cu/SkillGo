from __future__ import annotations

from pathlib import Path
from dataclasses import dataclass
from datetime import UTC, datetime

from .config import settings


class LocalObjectStorage:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def put(self, key: str, data: bytes) -> str:
        target = (self.root / key).resolve()
        if self.root not in target.parents:
            raise ValueError("invalid storage key")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        return key

    def read(self, key: str) -> bytes:
        target = (self.root / key).resolve()
        if self.root not in target.parents:
            raise ValueError("invalid storage key")
        return target.read_bytes()

    def delete(self, key: str) -> bool:
        target = (self.root / key).resolve()
        if self.root not in target.parents:
            raise ValueError("invalid storage key")
        try:
            target.unlink()
        except FileNotFoundError:
            return False
        return True

    def objects(self) -> list["StoredObject"]:
        objects: list[StoredObject] = []
        for target in self.root.rglob("*"):
            if not target.is_file():
                continue
            stat = target.stat()
            objects.append(
                StoredObject(
                    key=target.relative_to(self.root).as_posix(),
                    size_bytes=stat.st_size,
                    modified_at=datetime.fromtimestamp(stat.st_mtime, UTC),
                )
            )
        return objects


@dataclass(frozen=True)
class StoredObject:
    key: str
    size_bytes: int
    modified_at: datetime


storage = LocalObjectStorage(settings.storage_root)
