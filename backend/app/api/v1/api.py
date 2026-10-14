"""API v1 master router aggregating sub-routers."""

from fastapi import APIRouter

from app.api.v1.endpoints import health

api_router = APIRouter()

# Mount health check endpoints under /api/v1/health
api_router.include_router(health.router, prefix="/health", tags=["Health"])
