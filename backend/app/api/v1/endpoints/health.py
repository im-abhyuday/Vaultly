"""System health and readiness check endpoints."""

import logging
from typing import Any

import redis.asyncio as aioredis
from fastapi import APIRouter, status
from pydantic import BaseModel
from sqlalchemy import text

from app.core.config import get_settings
from app.db.session import engine

router = APIRouter()
logger = logging.getLogger(__name__)
settings = get_settings()


class ServiceStatus(BaseModel):
    status: str
    details: str | None = None


class HealthResponse(BaseModel):
    status: str
    environment: str
    version: str
    services: dict[str, ServiceStatus]


@router.get("", response_model=HealthResponse, status_code=status.HTTP_200_OK)
async def health_check() -> Any:
    """Detailed health check validating PostgreSQL and Redis connectivity."""
    services: dict[str, ServiceStatus] = {}
    all_healthy = True

    # Check PostgreSQL
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        services["database"] = ServiceStatus(status="healthy", details="PostgreSQL connection ok")
    except Exception as e:
        logger.warning(f"Database health check failed: {e}")
        services["database"] = ServiceStatus(status="unhealthy", details=str(e))
        all_healthy = False

    # Check Redis
    try:
        redis_client = aioredis.from_url(settings.REDIS_URL, decode_responses=True)
        await redis_client.ping()
        await redis_client.close()
        services["redis"] = ServiceStatus(status="healthy", details="Redis ping ok")
    except Exception as e:
        logger.warning(f"Redis health check failed: {e}")
        services["redis"] = ServiceStatus(status="unhealthy", details=str(e))
        all_healthy = False

    overall_status = "healthy" if all_healthy else "degraded"

    return {
        "status": overall_status,
        "environment": settings.ENVIRONMENT,
        "version": "0.1.0",
        "services": services,
    }
