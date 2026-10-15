"""Groups and group memberships API endpoints."""

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import delete, func, select
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import (
    CurrentUser,
    get_current_session,
    require_role,
)
from app.models.enums import RoleEnum
from app.models.group import Group
from app.models.group_member import GroupMember
from app.models.membership import Membership
from app.models.user import User
from app.schemas.group import (
    AddGroupMemberRequest,
    GroupCreateRequest,
    GroupMemberResponse,
    GroupResponse,
    GroupUpdateRequest,
)

router = APIRouter()


@router.get("", response_model=list[GroupResponse])
async def list_groups(
    _user: CurrentUser = Depends(require_role([RoleEnum.ADMIN, RoleEnum.MEMBER, RoleEnum.AUDITOR])),
    session: AsyncSession = Depends(get_current_session),
) -> list[GroupResponse]:
    """List all groups within the current tenant (available to any active member)."""
    stmt = (
        select(Group)
        .where(Group.tenant_id == _user.tenant_id)
        .order_by(Group.name.asc())
    )
    groups = (await session.execute(stmt)).scalars().all()
    return [GroupResponse.model_validate(g) for g in groups]


@router.post("", response_model=GroupResponse, status_code=status.HTTP_201_CREATED)
async def create_group(
    payload: GroupCreateRequest,
    current_user: CurrentUser = Depends(require_role([RoleEnum.ADMIN])),
    session: AsyncSession = Depends(get_current_session),
) -> GroupResponse:
    """Create a new group within the tenant (ADMIN only)."""
    # Check duplicate name within tenant
    existing = (
        await session.execute(
            select(Group.id).where(
                Group.tenant_id == current_user.tenant_id,
                func.lower(Group.name) == payload.name.lower(),
            )
        )
    ).scalar_one_or_none()
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"A group with name '{payload.name}' already exists in this tenant.",
        )

    group = Group(
        id=uuid.uuid4(),
        tenant_id=current_user.tenant_id,
        name=payload.name,
        description=payload.description,
    )
    session.add(group)
    await session.flush()

    return GroupResponse.model_validate(group)


@router.get("/{group_id}", response_model=GroupResponse)
async def get_group(
    group_id: uuid.UUID,
    current_user: CurrentUser = Depends(require_role([RoleEnum.ADMIN, RoleEnum.MEMBER, RoleEnum.AUDITOR])),
    session: AsyncSession = Depends(get_current_session),
) -> GroupResponse:
    """Retrieve details of a single group by ID (any member). Returns 404 if not found or cross-tenant."""
    stmt = select(Group).where(
        Group.tenant_id == current_user.tenant_id,
        Group.id == group_id,
    )
    group = (await session.execute(stmt)).scalar_one_or_none()
    if not group:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Group not found.",
        )
    return GroupResponse.model_validate(group)


@router.patch("/{group_id}", response_model=GroupResponse)
async def update_group(
    group_id: uuid.UUID,
    payload: GroupUpdateRequest,
    current_user: CurrentUser = Depends(require_role([RoleEnum.ADMIN])),
    session: AsyncSession = Depends(get_current_session),
) -> GroupResponse:
    """Update group details (ADMIN only). Returns 404 if not found or cross-tenant."""
    stmt = select(Group).where(
        Group.tenant_id == current_user.tenant_id,
        Group.id == group_id,
    )
    group = (await session.execute(stmt)).scalar_one_or_none()
    if not group:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Group not found.",
        )

    if payload.name is not None and payload.name.lower() != group.name.lower():
        existing = (
            await session.execute(
                select(Group.id).where(
                    Group.tenant_id == current_user.tenant_id,
                    func.lower(Group.name) == payload.name.lower(),
                    Group.id != group_id,
                )
            )
        ).scalar_one_or_none()
        if existing:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"A group with name '{payload.name}' already exists in this tenant.",
            )
        group.name = payload.name

    if payload.description is not None:
        group.description = payload.description

    return GroupResponse.model_validate(group)


@router.delete("/{group_id}", status_code=status.HTTP_200_OK)
async def delete_group(
    group_id: uuid.UUID,
    current_user: CurrentUser = Depends(require_role([RoleEnum.ADMIN])),
    session: AsyncSession = Depends(get_current_session),
) -> dict[str, str]:
    """Delete a group (ADMIN only). Returns 404 if not found or cross-tenant."""
    stmt = select(Group).where(
        Group.tenant_id == current_user.tenant_id,
        Group.id == group_id,
    )
    group = (await session.execute(stmt)).scalar_one_or_none()
    if not group:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Group not found.",
        )

    await session.delete(group)
    return {"message": "Group deleted successfully."}


@router.get("/{group_id}/members", response_model=list[GroupMemberResponse])
async def list_group_members(
    group_id: uuid.UUID,
    current_user: CurrentUser = Depends(require_role([RoleEnum.ADMIN, RoleEnum.MEMBER, RoleEnum.AUDITOR])),
    session: AsyncSession = Depends(get_current_session),
) -> list[GroupMemberResponse]:
    """List members belonging to a group (any active member)."""
    # Verify group exists in tenant
    grp = (
        await session.execute(
            select(Group.id).where(
                Group.tenant_id == current_user.tenant_id,
                Group.id == group_id,
            )
        )
    ).scalar_one_or_none()
    if not grp:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Group not found.",
        )

    stmt = (
        select(GroupMember, User, Membership)
        .join(
            Membership,
            (Membership.tenant_id == GroupMember.tenant_id) & (Membership.user_id == GroupMember.user_id),
        )
        .join(User, User.id == GroupMember.user_id)
        .where(
            GroupMember.tenant_id == current_user.tenant_id,
            GroupMember.group_id == group_id,
        )
    )
    rows = (await session.execute(stmt)).all()

    return [
        GroupMemberResponse(
            user_id=user.id,
            email=user.email,
            full_name=user.full_name,
            role=mem.role,
        )
        for gm, user, mem in rows
    ]


@router.post("/{group_id}/members", status_code=status.HTTP_201_CREATED)
async def add_group_member(
    group_id: uuid.UUID,
    payload: AddGroupMemberRequest,
    current_user: CurrentUser = Depends(require_role([RoleEnum.ADMIN])),
    session: AsyncSession = Depends(get_current_session),
) -> dict[str, str]:
    """Add a tenant member to a group (ADMIN only). Rejects cross-tenant users with 404."""
    # Verify group exists in tenant
    grp = (
        await session.execute(
            select(Group.id).where(
                Group.tenant_id == current_user.tenant_id,
                Group.id == group_id,
            )
        )
    ).scalar_one_or_none()
    if not grp:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Group not found.",
        )

    # Verify user is an active member of THIS tenant
    mem = (
        await session.execute(
            select(Membership.id).where(
                Membership.tenant_id == current_user.tenant_id,
                Membership.user_id == payload.user_id,
                Membership.is_active == True,  # noqa: E712
            )
        )
    ).scalar_one_or_none()
    if not mem:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User is not an active member of this tenant.",
        )

    # Check if already in group
    existing = (
        await session.execute(
            select(GroupMember).where(
                GroupMember.tenant_id == current_user.tenant_id,
                GroupMember.group_id == group_id,
                GroupMember.user_id == payload.user_id,
            )
        )
    ).scalar_one_or_none()
    if existing:
        return {"message": "User is already a member of this group."}

    gm = GroupMember(
        tenant_id=current_user.tenant_id,
        group_id=group_id,
        user_id=payload.user_id,
    )
    session.add(gm)

    return {"message": "Member added to group successfully."}


@router.delete("/{group_id}/members/{user_id}", status_code=status.HTTP_200_OK)
async def remove_group_member(
    group_id: uuid.UUID,
    user_id: uuid.UUID,
    current_user: CurrentUser = Depends(require_role([RoleEnum.ADMIN])),
    session: AsyncSession = Depends(get_current_session),
) -> dict[str, str]:
    """Remove a user from a group (ADMIN only)."""
    # Verify group exists
    grp = (
        await session.execute(
            select(Group.id).where(
                Group.tenant_id == current_user.tenant_id,
                Group.id == group_id,
            )
        )
    ).scalar_one_or_none()
    if not grp:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Group not found.",
        )

    stmt = delete(GroupMember).where(
        GroupMember.tenant_id == current_user.tenant_id,
        GroupMember.group_id == group_id,
        GroupMember.user_id == user_id,
    )
    res = await session.execute(stmt)
    if isinstance(res, CursorResult) and res.rowcount == 0:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User is not a member of this group.",
        )

    return {"message": "User removed from group successfully."}
