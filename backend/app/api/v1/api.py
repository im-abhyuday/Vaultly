"""API v1 master router aggregating sub-routers."""

from fastapi import APIRouter

from app.api.v1.endpoints import auth, documents, groups, health, users

api_router = APIRouter()

# Mount health check endpoints under /api/v1/health
api_router.include_router(health.router, prefix="/health", tags=["Health"])

# Mount auth endpoints under /api/v1/auth
api_router.include_router(auth.router, prefix="/auth", tags=["Auth"])

# Mount users endpoints under /api/v1/users
api_router.include_router(users.router, prefix="/users", tags=["Users"])

# Mount groups endpoints under /api/v1/groups
api_router.include_router(groups.router, prefix="/groups", tags=["Groups"])

# Mount documents endpoints under /api/v1/documents
api_router.include_router(documents.router, prefix="/documents", tags=["Documents"])
