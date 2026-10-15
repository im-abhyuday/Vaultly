"""Pydantic validation schemas for document management and chunk retrieval."""

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.models.enums import DocumentVisibility


class DocumentResponse(BaseModel):
    """Document metadata and current processing state."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    tenant_id: uuid.UUID
    owner_id: uuid.UUID
    filename: str
    detected_type: str
    size_bytes: int
    checksum_sha256: str
    status: str
    error_code: str | None = None
    error_message: str | None = None
    failed_stage: str | None = None
    attempts: int
    visibility: str
    chunking_config_id: uuid.UUID
    page_count: int | None = None
    chunk_count: int = 0
    created_at: datetime
    updated_at: datetime
    parsed_at: datetime | None = None
    indexed_at: datetime | None = None
    duplicate: bool = False


class DocumentAccessUpdateRequest(BaseModel):
    """Payload to update document visibility and ACL entries."""

    visibility: DocumentVisibility
    group_ids: list[uuid.UUID] = Field(default_factory=list)
    user_ids: list[uuid.UUID] = Field(default_factory=list)


class ChunkResponse(BaseModel):
    """Document text segment details (excluding vector embeddings)."""

    model_config = ConfigDict(from_attributes=True, populate_by_name=True)

    id: uuid.UUID
    document_id: uuid.UUID
    ordinal: int
    text: str
    token_count: int
    chunk_metadata: dict[str, Any] = Field(default_factory=dict, serialization_alias="metadata")
    created_at: datetime


class DocumentListResponse(BaseModel):
    """Paginated list of document metadata."""

    items: list[DocumentResponse]
    total: int
    page: int
    page_size: int
