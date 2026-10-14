"""Unit tests for health endpoints and middleware."""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import AsyncClient


async def test_root_endpoint(async_client: AsyncClient) -> None:
    """Verify root endpoint returns service identity."""
    response = await async_client.get("/")
    assert response.status_code == 200
    data = response.json()
    assert data["service"] == "TenantRAG"
    assert data["version"] == "0.1.0"
    assert data["docs"] == "/api/v1/docs"


async def test_liveness_probe(async_client: AsyncClient) -> None:
    """Verify /healthz returns 200 OK for Kubernetes/container liveness check."""
    response = await async_client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_request_id_and_timing_headers(async_client: AsyncClient) -> None:
    """Verify middleware injects X-Request-ID and X-Response-Time-MS headers."""
    custom_id = "test-request-id-12345"
    response = await async_client.get("/healthz", headers={"X-Request-ID": custom_id})
    assert response.status_code == 200
    assert response.headers.get("X-Request-ID") == custom_id
    assert "X-Response-Time-MS" in response.headers


@pytest.mark.asyncio
async def test_readyz_success(async_client: AsyncClient) -> None:
    """Verify /readyz returns 200 OK when database is reachable."""
    mock_conn = AsyncMock()
    mock_conn.execute = AsyncMock()

    @asynccontextmanager
    async def mock_connect() -> AsyncGenerator[AsyncMock, None]:
        yield mock_conn

    mock_engine = MagicMock()
    mock_engine.connect = mock_connect

    with patch("app.main.engine", mock_engine):
        response = await async_client.get("/readyz")
        assert response.status_code == 200
        assert response.json() == {"status": "ready"}


@pytest.mark.asyncio
async def test_readyz_database_failure(async_client: AsyncClient) -> None:
    """Verify /readyz returns 503 Service Unavailable when database connection fails."""

    @asynccontextmanager
    async def mock_connect_fail() -> AsyncGenerator[None, None]:
        raise Exception("Database connection timeout")
        yield  # unreachable, marks as async generator

    mock_engine = MagicMock()
    mock_engine.connect = mock_connect_fail

    with patch("app.main.engine", mock_engine):
        response = await async_client.get("/readyz")
        assert response.status_code == 503
        data = response.json()
        assert data["status"] == "not_ready"
        assert "Database connection timeout" in data["error"]


@pytest.mark.asyncio
async def test_api_v1_health_healthy(async_client: AsyncClient) -> None:
    """Verify /api/v1/health reports healthy status when all dependencies respond."""
    mock_conn = AsyncMock()
    mock_conn.execute = AsyncMock()

    @asynccontextmanager
    async def mock_connect() -> AsyncGenerator[AsyncMock, None]:
        yield mock_conn

    mock_engine = MagicMock()
    mock_engine.connect = mock_connect

    mock_redis = AsyncMock()
    mock_redis.ping = AsyncMock(return_value=True)
    mock_redis.close = AsyncMock()

    with (
        patch("app.api.v1.endpoints.health.engine", mock_engine),
        patch("app.api.v1.endpoints.health.aioredis.from_url", return_value=mock_redis),
    ):
        response = await async_client.get("/api/v1/health")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "healthy"
        assert data["services"]["database"]["status"] == "healthy"
        assert data["services"]["redis"]["status"] == "healthy"


@pytest.mark.asyncio
async def test_api_v1_health_degraded(async_client: AsyncClient) -> None:
    """Verify /api/v1/health reports degraded status when a dependency fails."""
    mock_conn = AsyncMock()
    mock_conn.execute = AsyncMock()

    @asynccontextmanager
    async def mock_connect() -> AsyncGenerator[AsyncMock, None]:
        yield mock_conn

    mock_engine = MagicMock()
    mock_engine.connect = mock_connect

    with (
        patch("app.api.v1.endpoints.health.engine", mock_engine),
        patch(
            "app.api.v1.endpoints.health.aioredis.from_url",
            side_effect=Exception("Redis connection refused"),
        ),
    ):
        response = await async_client.get("/api/v1/health")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "degraded"
        assert data["services"]["database"]["status"] == "healthy"
        assert data["services"]["redis"]["status"] == "unhealthy"
