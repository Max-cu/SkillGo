from __future__ import annotations

from pathlib import Path

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


storage = LocalObjectStorage(settings.storage_root)
