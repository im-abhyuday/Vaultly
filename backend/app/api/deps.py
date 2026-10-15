"""Authentication and authorization dependencies for FastAPI endpoints.

Provides CurrentUser context, JWT bearer extraction, tenant session scoping,
and role-based access control (RBAC).
"""

import uuid
from collections.abc import AsyncGenerator, Callable
from dataclasses import dataclass
from typing import Any

from fastapi import Depends, HTTPException, Security, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import tenant_id_ctx, user_id_ctx
from app.core.security import decode_access_token, is_token_denylisted
from app.db.session import async_session_maker, set_tenant_context
from app.models.enums import RoleEnum
from app.models.membership import Membership
from app.models.tenant import Tenant
from app.models.user import User

bearer_scheme = HTTPBearer(auto_error=True)


@dataclass(frozen=True)
class CurrentUser:
    """Authenticated user context for the active tenant."""

    user_id: uuid.UUID
    tenant_id: uuid.UUID
    role: RoleEnum
    email: str
    full_name: str
    token_jti: str


async def get_current_user_and_session(
    credentials: HTTPAuthorizationCredentials = Security(bearer_scheme),
) -> AsyncGenerator[tuple[CurrentUser, AsyncSession], None]:
    """Dependency verifying access JWT, scoping tenant session, and returning user context.

    Enforces that:
    1. JWT is valid, correctly signed, unexpired, and not denylisted in Redis.
    2. Tenant session is opened with transaction-local tenant context.
    3. User exists, tenant exists and is active, and membership exists and is active.
    4. Current role is loaded from DB (not JWT claims) for instantaneous role updates.
    """
    token = credentials.credentials
    try:
        payload = decode_access_token(token)
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Invalid authentication token: {e}",
            headers={"WWW-Authenticate": "Bearer"},
        ) from e

    jti = payload["jti"]
    if await is_token_denylisted(jti):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token has been revoked.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    try:
        user_id = uuid.UUID(payload["sub"])
        tenant_id = uuid.UUID(payload["tid"])
    except (ValueError, KeyError) as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Malformed token claims.",
            headers={"WWW-Authenticate": "Bearer"},
        ) from e

    # Bind logging context
    t_token = tenant_id_ctx.set(str(tenant_id))
    u_token = user_id_ctx.set(str(user_id))

    async with async_session_maker() as session:
        async with session.begin():
            # Set transaction-local tenant context for RLS
            await set_tenant_context(session, tenant_id)

            # Verify tenant is active
            tenant_res = await session.execute(
                select(Tenant).where(Tenant.id == tenant_id, Tenant.is_active == True)  # noqa: E712
            )
            tenant = tenant_res.scalar_one_or_none()
            if not tenant:
                tenant_id_ctx.reset(t_token)
                user_id_ctx.reset(u_token)
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Tenant account is inactive or not found.",
                    headers={"WWW-Authenticate": "Bearer"},
                )

            # Verify membership is active and fetch role from database
            stmt = (
                select(Membership, User)
                .join(User, User.id == Membership.user_id)
                .where(
                    Membership.tenant_id == tenant_id,
                    Membership.user_id == user_id,
                    Membership.is_active == True,  # noqa: E712
                    User.is_active == True,  # noqa: E712
                )
            )
            result = (await session.execute(stmt)).first()
            if not result:
                tenant_id_ctx.reset(t_token)
                user_id_ctx.reset(u_token)
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="User does not have an active membership in this tenant.",
                    headers={"WWW-Authenticate": "Bearer"},
                )

            membership, user = result
            try:
                role_enum = RoleEnum(membership.role)
            except ValueError:
                role_enum = RoleEnum.MEMBER

            current_user = CurrentUser(
                user_id=user.id,
                tenant_id=tenant.id,
                role=role_enum,
                email=user.email,
                full_name=user.full_name,
                token_jti=jti,
            )

            try:
                yield current_user, session
            finally:
                tenant_id_ctx.reset(t_token)
                user_id_ctx.reset(u_token)


async def get_current_user(
    user_and_session: tuple[CurrentUser, AsyncSession] = Depends(get_current_user_and_session),
) -> CurrentUser:
    """Dependency extracting CurrentUser context."""
    return user_and_session[0]


async def get_current_session(
    user_and_session: tuple[CurrentUser, AsyncSession] = Depends(get_current_user_and_session),
) -> AsyncSession:
    """Dependency extracting the active tenant-scoped AsyncSession."""
    return user_and_session[1]


def require_role(allowed_roles: list[RoleEnum]) -> Callable[..., Any]:
    """Dependency factory checking that CurrentUser has one of the allowed roles.

    Raises 403 Forbidden if user lacks sufficient privileges.
    """

    async def role_checker(
        current_user: CurrentUser = Depends(get_current_user),
    ) -> CurrentUser:
        if current_user.role not in allowed_roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Operation not permitted for role '{current_user.role.value}'. Requires one of {[r.value for r in allowed_roles]}",
            )
        return current_user

    return role_checker
