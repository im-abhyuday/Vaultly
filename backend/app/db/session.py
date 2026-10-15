import uuid
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from app.core.config import get_settings

settings = get_settings()

# Asynchronous database engine (connects as runtime role tenantrag_app)
engine_kwargs: dict[str, Any] = {
    "echo": False,
    "future": True,
    "connect_args": {
        "server_settings": {"jit": "off"},
    },
}
if settings.is_testing:
    engine_kwargs["poolclass"] = NullPool
else:
    engine_kwargs["pool_pre_ping"] = True
    engine_kwargs["pool_size"] = 10
    engine_kwargs["max_overflow"] = 20

engine: AsyncEngine = create_async_engine(
    str(settings.DATABASE_URL),
    **engine_kwargs,
)



# Async session factory
async_session_maker: async_sessionmaker[AsyncSession] = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    autocommit=False,
    autoflush=False,
    expire_on_commit=False,
)


async def set_tenant_context(session: AsyncSession, tenant_id: uuid.UUID) -> None:
    """Set transaction-local tenant context using set_config.

    Using SELECT set_config('app.tenant_id', :tid, true) sets the setting
    strictly for the current transaction (is_local=true), ensuring it never leaks
    across pooled connections once the transaction completes.
    """
    await session.execute(
        text("SELECT set_config('app.tenant_id', :tid, true)"),
        {"tid": str(tenant_id)},
    )


@asynccontextmanager
async def tenant_transaction(
    tenant_id: uuid.UUID,
    session_maker: async_sessionmaker[AsyncSession] | None = None,
) -> AsyncGenerator[AsyncSession, None]:
    """Context manager opening a transaction, setting the tenant context, and committing."""
    maker = session_maker or async_session_maker
    async with maker() as session:
        async with session.begin():
            await set_tenant_context(session, tenant_id)
            yield session


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency yielding a generic async database session."""
    async with async_session_maker() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()

