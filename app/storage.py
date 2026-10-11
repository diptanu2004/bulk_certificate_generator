"""Where rendered PDFs live.

A two-method interface with a local-filesystem implementation. This is the
seam for running more than one API replica: swapping in an S3 implementation
(and serving presigned URLs instead of bytes) touches nothing else.
"""

from pathlib import Path
from typing import Protocol

from app.config import settings


class Storage(Protocol):
    def write(self, key: str, data: bytes) -> None: ...

    def read(self, key: str) -> bytes: ...


class LocalStorage:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def _resolve(self, key: str) -> Path:
        path = (self.root / key).resolve()
        # Keys are built from UUIDs, but never trust a key enough to escape root.
        if not path.is_relative_to(self.root.resolve()):
            raise ValueError(f"invalid storage key: {key!r}")
        return path

    def write(self, key: str, data: bytes) -> None:
        path = self._resolve(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def read(self, key: str) -> bytes:
        return self._resolve(key).read_bytes()


def storage_key(job_id, certificate_id) -> str:
    return f"{job_id}/{certificate_id}.pdf"


def get_storage() -> Storage:
    return LocalStorage(settings.storage_dir)
