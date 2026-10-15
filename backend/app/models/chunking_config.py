"""Chunking configuration ORM model."""

import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, UUIDPrimaryKeyMixin


class ChunkingConfig(Base, UUIDPrimaryKeyMixin):
    """Configuration for document chunking and vector embedding strategies."""

    __tablename__ = "chunking_configs"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
    )
    name: Mapped[str] = mapped_column(String(64), default="default", nullable=False)
    strategy: Mapped[str] = mapped_column(String(32), default="recursive", nullable=False)
    chunk_size_tokens: Mapped[int] = mapped_column(Integer, default=256, nullable=False)
    chunk_overlap_tokens: Mapped[int] = mapped_column(Integer, default=32, nullable=False)
    embedding_model: Mapped[str] = mapped_column(
        String(128), default="BAAI/bge-small-en-v1.5", nullable=False
    )
    embedding_dim: Mapped[int] = mapped_column(Integer, default=384, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        nullable=False,
        server_default=func.now(),
    )

    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_chunking_configs_tenant_id"),
        Index("ix_chunking_configs_tenant_id", "tenant_id"),
        Index(
            "uq_chunking_configs_active_tenant",
            "tenant_id",
            unique=True,
            postgresql_where=text("is_active = true"),
        ),
    )
