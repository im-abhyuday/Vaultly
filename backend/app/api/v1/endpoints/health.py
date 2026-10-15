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


async def check_kafka() -> tuple[bool, str]:
    """Check Kafka broker connectivity and cluster metadata."""
    try:
        from aiokafka.admin import AIOKafkaAdminClient

        admin_client = AIOKafkaAdminClient(
            bootstrap_servers=settings.KAFKA_BOOTSTRAP_SERVERS,
            request_timeout_ms=2000,
        )
        await admin_client.start()
        try:
            cluster_info = await admin_client.describe_cluster()
            broker_count = len(cluster_info.get("brokers", []))
            return True, f"Kafka ok ({broker_count} brokers)"
        finally:
            await admin_client.close()
    except Exception as e:
        return False, str(e)


async def check_outbox() -> ServiceStatus | None:
    """Report unpublished outbox events count if outbox_events table exists."""
    try:
        async with engine.connect() as conn:
            check_table = await conn.execute(
                text("SELECT 1 FROM information_schema.tables WHERE table_name = 'outbox_events'")
            )
            if check_table.scalar():
                count_res = await conn.execute(
                    text("SELECT count(*) FROM outbox_events WHERE published_at IS NULL")
                )
                count = count_res.scalar() or 0
                return ServiceStatus(status="healthy", details=f"{count} unpublished events")
    except Exception as e:
        logger.warning(f"Outbox table check failed: {e}")
        return ServiceStatus(status="unhealthy", details=str(e))
    return None


@router.get("", response_model=HealthResponse, status_code=status.HTTP_200_OK)
async def health_check() -> Any:
    """Detailed health check validating PostgreSQL, Redis, Kafka, and Outbox."""
    services: dict[str, ServiceStatus] = {}
    all_healthy = True

    # 1. Check PostgreSQL
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        services["database"] = ServiceStatus(status="healthy", details="PostgreSQL connection ok")
    except Exception as e:
        logger.warning(f"Database health check failed: {e}")
        services["database"] = ServiceStatus(status="unhealthy", details=str(e))
        all_healthy = False

    # 2. Check Redis
    try:
        redis_client = aioredis.from_url(settings.REDIS_URL, decode_responses=True)
        await redis_client.ping()
        await redis_client.close()
        services["redis"] = ServiceStatus(status="healthy", details="Redis ping ok")
    except Exception as e:
        logger.warning(f"Redis health check failed: {e}")
        services["redis"] = ServiceStatus(status="unhealthy", details=str(e))
        all_healthy = False

    # 3. Check Kafka
    kafka_ok, kafka_details = await check_kafka()
    if kafka_ok:
        services["kafka"] = ServiceStatus(status="healthy", details=kafka_details)
    else:
        logger.warning(f"Kafka health check failed: {kafka_details}")
        services["kafka"] = ServiceStatus(status="unhealthy", details=kafka_details)
        all_healthy = False

    # 4. Check Outbox (when table exists)
    outbox_status = await check_outbox()
    if outbox_status:
        services["outbox"] = outbox_status
        if outbox_status.status != "healthy":
            all_healthy = False

    overall_status = "healthy" if all_healthy else "degraded"

    return {
        "status": overall_status,
        "environment": settings.ENVIRONMENT,
        "version": "0.1.0",
        "services": services,
    }
