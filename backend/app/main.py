"""TenantRAG FastAPI application entrypoint.

Initializes middleware, lifespan lifecycle, structured logging, and routers.
"""

import logging
import time
import uuid
from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.api.v1.api import api_router
from app.core.config import get_settings
from app.core.logging import request_id_ctx, setup_logging
from app.db.session import engine

settings = get_settings()
setup_logging(log_level=settings.LOG_LEVEL)
logger = logging.getLogger("tenantrag.api")


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncGenerator[None, None]:
    """Lifespan context manager handling application startup and shutdown."""
    logger.info(
        "Starting Vaultly API",
        extra={"environment": settings.ENVIRONMENT, "debug": settings.DEBUG},
    )
    # Security assertion: ensure runtime role has neither superuser nor bypassrls
    async with engine.connect() as conn:
        result = await conn.execute(
            text(
                "SELECT rolname, rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user"
            )
        )
        role_row = result.mappings().first()
        if role_row:
            rolname = role_row["rolname"]
            rolsuper = role_row["rolsuper"]
            rolbypassrls = role_row["rolbypassrls"]
            if rolsuper or rolbypassrls:
                raise RuntimeError(
                    f"Security violation: Database runtime role '{rolname}' must NOT be "
                    f"superuser or have bypassrls (rolsuper={rolsuper}, rolbypassrls={rolbypassrls}). "
                    "Refusing to start."
                )
    yield
    logger.info("Shutting down Vaultly API")
    await engine.dispose()


app = FastAPI(
    title=settings.PROJECT_NAME,
    description="Multi-tenant RAG platform with built-in evaluation, access control, and semantic caching",
    version="0.1.0",
    openapi_url=f"{settings.API_V1_STR}/openapi.json",
    docs_url=f"{settings.API_V1_STR}/docs",
    redoc_url=f"{settings.API_V1_STR}/redoc",
    lifespan=lifespan,
)

# Cross-Origin Resource Sharing (CORS)
if settings.CORS_ORIGINS:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.CORS_ORIGINS,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )


@app.middleware("http")
async def request_logging_middleware(
    request: Request,
    call_next: Callable[[Request], Any],
) -> Response:
    """Inject request ID, track execution latency, and emit structured access log."""
    req_id = request.headers.get("X-Request-ID", str(uuid.uuid4()))
    token = request_id_ctx.set(req_id)
    start_time = time.perf_counter()

    try:
        response: Response = await call_next(request)
        duration_ms = round((time.perf_counter() - start_time) * 1000, 2)
        response.headers["X-Request-ID"] = req_id
        response.headers["X-Response-Time-MS"] = str(duration_ms)

        logger.info(
            f"{request.method} {request.url.path} {response.status_code} in {duration_ms}ms",
            extra={
                "method": request.method,
                "path": request.url.path,
                "status_code": response.status_code,
                "duration_ms": duration_ms,
            },
        )
        return response
    except Exception as exc:
        duration_ms = round((time.perf_counter() - start_time) * 1000, 2)
        logger.exception(
            f"Unhandled exception during {request.method} {request.url.path}: {exc}",
            extra={"duration_ms": duration_ms},
        )
        raise
    finally:
        request_id_ctx.reset(token)


# Top-level health and readiness probes
@app.get("/", tags=["Root"])
async def root() -> dict[str, str]:
    """Root endpoint returning service identity."""
    return {
        "service": settings.PROJECT_NAME,
        "version": "0.1.0",
        "docs": f"{settings.API_V1_STR}/docs",
    }


@app.get("/healthz", tags=["Health"], status_code=status.HTTP_200_OK)
async def liveness_probe() -> dict[str, str]:
    """Lightweight liveness probe for container orchestration."""
    return {"status": "ok"}


@app.get("/readyz", tags=["Health"])
async def readiness_probe() -> JSONResponse:
    """Readiness probe checking database connectivity before traffic routing."""
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return JSONResponse(status_code=status.HTTP_200_OK, content={"status": "ready"})
    except Exception as e:
        logger.warning(f"Readiness check failed: {e}")
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={"status": "not_ready", "error": str(e)},
        )


# Mount versioned API router
app.include_router(api_router, prefix=settings.API_V1_STR)
