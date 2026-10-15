"""Users and memberships management endpoints."""

import logging
import uuid
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import (
    CurrentUser,
    get_current_session,
    require_role,
)
from app.core.config import get_settings
from app.core.security import generate_opaque_token
from app.models.enums import RoleEnum
from app.models.invitation import Invitation
from app.models.membership import Membership
from app.models.refresh_token import RefreshToken
from app.models.user import User
from app.schemas.user import (
    InviteUserRequest,
    InviteUserResponse,
    UpdateUserRoleRequest,
    UserResponse,
)

router = APIRouter()
logger = logging.getLogger("vaultly.users")
settings = get_settings()


@router.get("", response_model=list[UserResponse])
async def list_tenant_users(
    _user: CurrentUser = Depends(require_role([RoleEnum.ADMIN, RoleEnum.AUDITOR])),
    session: AsyncSession = Depends(get_current_session),
) -> list[UserResponse]:
    """List all members of the active tenant (ADMIN or AUDITOR)."""
    stmt = (
        select(Membership, User)
        .join(User, User.id == Membership.user_id)
        .where(Membership.tenant_id == _user.tenant_id)
        .order_by(Membership.created_at.desc())
    )
    rows = (await session.execute(stmt)).all()

    return [
        UserResponse(
            id=user.id,
            email=user.email,
            full_name=user.full_name,
            role=mem.role,
            is_active=mem.is_active,
            created_at=mem.created_at,
        )
        for mem, user in rows
    ]


@router.post("/invite", response_model=InviteUserResponse, status_code=status.HTTP_201_CREATED)
async def invite_user(
    payload: InviteUserRequest,
    current_user: CurrentUser = Depends(require_role([RoleEnum.ADMIN])),
    session: AsyncSession = Depends(get_current_session),
) -> InviteUserResponse:
    """Create invitation to join tenant. Returns invite token in response (ADMIN only)."""
    # Check if already a member
    existing_mem = (
        await session.execute(
            select(Membership)
            .join(User, User.id == Membership.user_id)
            .where(
                Membership.tenant_id == current_user.tenant_id,
                func.lower(User.email) == payload.email.lower(),
                Membership.is_active == True,  # noqa: E712
            )
        )
    ).scalar_one_or_none()
    if existing_mem:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"User with email '{payload.email}' is already an active member of this tenant.",
        )

    # Generate token
    full_token, token_hash = generate_opaque_token(current_user.tenant_id)
    inv_id = uuid.uuid4()
    exp = datetime.now(UTC) + timedelta(days=7)

    invitation = Invitation(
        id=inv_id,
        tenant_id=current_user.tenant_id,
        email=payload.email.lower(),
        role=payload.role.value,
        token_hash=token_hash,
        invited_by=current_user.user_id,
        expires_at=exp,
    )
    session.add(invitation)

    logger.info(f"Invitation created for {payload.email} (id={inv_id})")

    return InviteUserResponse(
        id=inv_id,
        tenant_id=current_user.tenant_id,
        email=payload.email.lower(),
        role=payload.role.value,
        invite_token=full_token,
        expires_at=exp,
    )


@router.patch("/{user_id}/role", response_model=UserResponse)
async def update_member_role(
    user_id: uuid.UUID,
    payload: UpdateUserRoleRequest,
    current_user: CurrentUser = Depends(require_role([RoleEnum.ADMIN])),
    session: AsyncSession = Depends(get_current_session),
) -> UserResponse:
    """Update role of a tenant member. Prevents demoting the last active ADMIN."""
    stmt = (
        select(Membership, User)
        .join(User, User.id == Membership.user_id)
        .where(
            Membership.tenant_id == current_user.tenant_id,
            Membership.user_id == user_id,
        )
    )
    row = (await session.execute(stmt)).first()
    if not row:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found in this tenant.",
        )
    membership, user = row

    # Guard: Never demote the last ADMIN
    if membership.role == RoleEnum.ADMIN.value and payload.role != RoleEnum.ADMIN:
        admin_count = (
            await session.execute(
                select(func.count(Membership.id)).where(
                    Membership.tenant_id == current_user.tenant_id,
                    Membership.role == RoleEnum.ADMIN.value,
                    Membership.is_active == True,  # noqa: E712
                )
            )
        ).scalar() or 0
        if admin_count <= 1:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Cannot demote the last remaining ADMIN of the tenant.",
            )

    membership.role = payload.role.value

    return UserResponse(
        id=user.id,
        email=user.email,
        full_name=user.full_name,
        role=membership.role,
        is_active=membership.is_active,
        created_at=membership.created_at,
    )


@router.delete("/{user_id}", status_code=status.HTTP_200_OK)
async def remove_member(
    user_id: uuid.UUID,
    current_user: CurrentUser = Depends(require_role([RoleEnum.ADMIN])),
    session: AsyncSession = Depends(get_current_session),
) -> dict[str, str]:
    """Deactivate membership and revoke refresh tokens. Prevents removing last ADMIN."""
    stmt = (
        select(Membership)
        .where(
            Membership.tenant_id == current_user.tenant_id,
            Membership.user_id == user_id,
        )
    )
    membership = (await session.execute(stmt)).scalar_one_or_none()
    if not membership:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found in this tenant.",
        )

    # Guard: Never remove the last ADMIN
    if membership.role == RoleEnum.ADMIN.value and membership.is_active:
        admin_count = (
            await session.execute(
                select(func.count(Membership.id)).where(
                    Membership.tenant_id == current_user.tenant_id,
                    Membership.role == RoleEnum.ADMIN.value,
                    Membership.is_active == True,  # noqa: E712
                )
            )
        ).scalar() or 0
        if admin_count <= 1:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Cannot remove or deactivate the last remaining ADMIN of the tenant.",
            )

    # Deactivate membership
    membership.is_active = False

    # Revoke all refresh tokens for user in this tenant
    await session.execute(
        update(RefreshToken)
        .where(
            RefreshToken.tenant_id == current_user.tenant_id,
            RefreshToken.user_id == user_id,
        )
        .values(revoked_at=datetime.now(UTC))
    )

    return {"message": "User membership deactivated successfully."}
