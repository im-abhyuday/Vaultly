"""Pytest test fixtures and configuration for Vaultly."""

import os
from collections.abc import AsyncGenerator

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

# Test environment settings
os.environ["ENVIRONMENT"] = "test"
os.environ["POSTGRES_DB"] = "tenantrag_test"
os.environ["MIGRATION_DATABASE_URL"] = (
    "postgresql+asyncpg://tenantrag:tenantrag_secret@localhost:5432/tenantrag_test"
)
os.environ["DATABASE_URL"] = (
    "postgresql+asyncpg://tenantrag_app:tenantrag_app_secret@localhost:5432/tenantrag_test"
)
os.environ["APP_DB_USER"] = "tenantrag_app"
os.environ["APP_DB_PASSWORD"] = "tenantrag_app_secret"
os.environ["REDIS_URL"] = "redis://localhost:6379/1"
os.environ["JWT_SECRET_KEY"] = "test_secret_key_minimum_32_characters_long_for_testing!"

from app.core.config import Settings, get_settings
from app.main import app

OWNER_DATABASE_URL = os.environ["MIGRATION_DATABASE_URL"]
APP_DATABASE_URL = os.environ["DATABASE_URL"]


@pytest.fixture(scope="session")
def test_settings() -> Settings:
    """Return test settings instance."""
    return get_settings()


@pytest.fixture
async def owner_engine() -> AsyncGenerator[AsyncEngine, None]:
    """Engine connected as the migration owner role (superuser) for seeding and introspection."""
    engine = create_async_engine(
        OWNER_DATABASE_URL,
        future=True,
        pool_pre_ping=True,
    )
    yield engine
    await engine.dispose()


@pytest.fixture
async def app_engine() -> AsyncGenerator[AsyncEngine, None]:
    """Engine connected as the runtime unprivileged role (tenantrag_app) for all assertions."""
    engine = create_async_engine(
        APP_DATABASE_URL,
        future=True,
        pool_pre_ping=True,
    )
    yield engine
    await engine.dispose()


@pytest.fixture
def owner_session_maker(owner_engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Session factory for the owner role."""
    return async_sessionmaker(
        bind=owner_engine,
        class_=AsyncSession,
        autocommit=False,
        autoflush=False,
        expire_on_commit=False,
    )


@pytest.fixture
def app_session_maker(app_engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Session factory for the runtime app role."""
    return async_sessionmaker(
        bind=app_engine,
        class_=AsyncSession,
        autocommit=False,
        autoflush=False,
        expire_on_commit=False,
    )


@pytest.fixture(autouse=True)
async def clean_database(owner_engine: AsyncEngine) -> AsyncGenerator[None, None]:
    """Clean all tables before each test execution using the owner engine."""
    async with owner_engine.begin() as conn:
        await conn.execute(
            text(
                "TRUNCATE TABLE invitations, refresh_tokens, group_members, "
                "groups, memberships, users, tenants RESTART IDENTITY CASCADE;"
            )
        )
    yield


@pytest.fixture
async def async_client() -> AsyncGenerator[AsyncClient, None]:
    """Provide an asynchronous HTTP client configured for testing."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield client
