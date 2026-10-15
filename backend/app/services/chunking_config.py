"""Service for managing tenant chunking configurations."""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models.chunking_config import ChunkingConfig

settings = get_settings()


async def get_or_create_active_config(
    session: AsyncSession,
    tenant_id: uuid.UUID,
) -> ChunkingConfig:
    """Retrieve the active chunking configuration for a tenant, or lazily create one with system defaults."""
    stmt = select(ChunkingConfig).where(
        ChunkingConfig.tenant_id == tenant_id,
        ChunkingConfig.is_active == True,  # noqa: E712
    )
    config = (await session.execute(stmt)).scalar_one_or_none()
    if config:
        return config

    new_config = ChunkingConfig(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        name="default",
        strategy="recursive",
        chunk_size_tokens=settings.DEFAULT_CHUNK_SIZE_TOKENS,
        chunk_overlap_tokens=settings.DEFAULT_CHUNK_OVERLAP_TOKENS,
        embedding_model=settings.DEFAULT_EMBEDDING_MODEL,
        embedding_dim=settings.DEFAULT_EMBEDDING_DIM,
        is_active=True,
    )
    session.add(new_config)
    await session.flush()
    return new_config
