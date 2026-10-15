"""Unit tests for LocalStorage backend and path security."""

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from app.services.storage import LocalStorage


async def byte_generator(data: bytes, chunk_size: int = 4) -> AsyncIterator[bytes]:
    """Yield bytes in small chunks for testing put_stream."""
    for i in range(0, len(data), chunk_size):
        yield data[i : i + chunk_size]


@pytest.mark.asyncio
async def test_put_stream_atomic_write(tmp_path: Path) -> None:
    """Verify put_stream streams data atomically and writes correct content."""
    storage = LocalStorage(root_dir=tmp_path)
    rel_path = "tenants/tenant-1/documents/doc-1/original"
    content = b"Hello, Vaultly atomic storage!"

    written = await storage.put_stream(rel_path, byte_generator(content))
    assert written == len(content)
    assert storage.exists(rel_path)

    with storage.open(rel_path) as f:
        read_content = f.read()
    assert read_content == content


@pytest.mark.asyncio
async def test_path_traversal_prevention(tmp_path: Path) -> None:
    """Verify paths attempting to escape root raise ValueError."""
    storage = LocalStorage(root_dir=tmp_path)
    traversal_paths = [
        "../../etc/passwd",
        "/etc/shadow",
        "tenants/../../outside.txt",
        "nested/../../../evil.bin",
    ]

    for evil_path in traversal_paths:
        with pytest.raises(ValueError, match="Path traversal security violation"):
            await storage.put_stream(evil_path, byte_generator(b"evil"))

        with pytest.raises(ValueError, match="Path traversal security violation"):
            storage.open(evil_path)


def test_delete_prefix(tmp_path: Path) -> None:
    """Verify delete_prefix removes entire directory trees safely."""
    storage = LocalStorage(root_dir=tmp_path)
    file1 = tmp_path / "tenants" / "t1" / "doc1" / "original"
    file2 = tmp_path / "tenants" / "t1" / "doc1" / "parsed.json"
    file3 = tmp_path / "tenants" / "t1" / "doc2" / "original"

    for f in (file1, file2, file3):
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("dummy")

    assert storage.exists("tenants/t1/doc1/original")
    assert storage.exists("tenants/t1/doc2/original")

    # Delete doc1 prefix
    storage.delete_prefix("tenants/t1/doc1")

    assert not storage.exists("tenants/t1/doc1/original")
    assert not storage.exists("tenants/t1/doc1/parsed.json")
    # doc2 remains untouched
    assert storage.exists("tenants/t1/doc2/original")
