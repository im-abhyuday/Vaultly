"""Storage backend protocol and local filesystem storage implementation."""

import os
import shutil
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import BinaryIO, Protocol

from app.core.config import get_settings

settings = get_settings()


class StorageBackend(Protocol):
    """Storage protocol for document file persistence."""

    async def put_stream(self, path: str, stream: AsyncIterator[bytes]) -> int:
        """Stream content to path atomically, returning total bytes written."""
        ...

    def open(self, path: str) -> BinaryIO:
        """Open a read-only binary stream for a stored file."""
        ...

    def exists(self, path: str) -> bool:
        """Check if file exists in storage."""
        ...

    def delete_prefix(self, prefix: str) -> None:
        """Delete all files and folders under the specified path prefix."""
        ...


class LocalStorage:
    """Atomic local filesystem storage backend with path traversal protection."""

    def __init__(self, root_dir: str | Path | None = None) -> None:
        configured_root = root_dir or settings.STORAGE_LOCAL_ROOT
        self.root_path = Path(configured_root).resolve()
        self.root_path.mkdir(parents=True, exist_ok=True)

    def _resolve_and_verify(self, relative_path: str) -> Path:
        """Resolve path and verify it stays strictly within the root directory."""
        path_obj = Path(relative_path)
        if path_obj.is_absolute() or relative_path.startswith(("/", "\\")):
            raise ValueError(
                f"Path traversal security violation: '{relative_path}' is an absolute path or has root slash."
            )

        resolved = (self.root_path / path_obj).resolve()

        if resolved == self.root_path:
            raise ValueError(
                f"Path traversal security violation: '{relative_path}' refers to storage root itself."
            )

        try:
            resolved.relative_to(self.root_path)
        except ValueError as err:
            raise ValueError(
                f"Path traversal security violation: '{relative_path}' escapes storage root."
            ) from err

        return resolved

    async def put_stream(self, path: str, stream: AsyncIterator[bytes]) -> int:
        """Atomically stream bytes to disk using a temporary file and atomic replace."""
        target_path = self._resolve_and_verify(path)
        target_path.parent.mkdir(parents=True, exist_ok=True)

        temp_path = target_path.with_suffix(f".tmp.{uuid.uuid4().hex}")
        total_bytes = 0

        try:
            with temp_path.open("wb") as f:
                async for chunk in stream:
                    if chunk:
                        f.write(chunk)
                        total_bytes += len(chunk)
                f.flush()
                os.fsync(f.fileno())

            # Atomic replace on POSIX and Windows (Python 3.3+)
            os.replace(temp_path, target_path)
            return total_bytes
        except Exception:
            if temp_path.exists():
                try:
                    temp_path.unlink()
                except OSError:
                    pass
            raise

    def open(self, path: str) -> BinaryIO:
        """Open a readable binary stream for the specified storage path."""
        target_path = self._resolve_and_verify(path)
        if not target_path.is_file():
            raise FileNotFoundError(f"Storage object '{path}' not found.")
        return target_path.open("rb")

    def exists(self, path: str) -> bool:
        """Return True if path exists as a regular file."""
        try:
            target_path = self._resolve_and_verify(path)
            return target_path.is_file()
        except ValueError:
            return False

    def delete_prefix(self, prefix: str) -> None:
        """Delete the file or directory tree under the specified prefix."""
        try:
            target_path = self._resolve_and_verify(prefix)
            if target_path.is_dir():
                shutil.rmtree(target_path, ignore_errors=True)
            elif target_path.is_file():
                target_path.unlink(missing_ok=True)
        except ValueError:
            pass


def get_document_storage_key(
    tenant_id: uuid.UUID,
    document_id: uuid.UUID,
    artifact: str = "original",
) -> str:
    """Generate canonical storage layout key for a document artifact."""
    return f"tenants/{tenant_id}/documents/{document_id}/{artifact}"


_storage_singleton: StorageBackend | None = None


def get_storage() -> StorageBackend:
    """Return default storage backend instance."""
    global _storage_singleton
    if _storage_singleton is None:
        _storage_singleton = LocalStorage()
    return _storage_singleton
