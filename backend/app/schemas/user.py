"""Pydantic validation schemas for user and membership management."""

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.models.enums import RoleEnum

EMAIL_REGEX = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"


class UserResponse(BaseModel):
    """Details of a user belonging to the current tenant."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    email: str
    full_name: str
    role: str
    is_active: bool
    created_at: datetime


class InviteUserRequest(BaseModel):
    """Payload to invite a new user to the tenant."""

    email: str = Field(pattern=EMAIL_REGEX, min_length=5, max_length=255)
    role: RoleEnum = RoleEnum.MEMBER


class InviteUserResponse(BaseModel):
    """Response containing invitation metadata and invite token."""

    id: uuid.UUID
    tenant_id: uuid.UUID
    email: str
    role: str
    invite_token: str
    expires_at: datetime


class UpdateUserRoleRequest(BaseModel):
    """Payload to change a member's role within the tenant."""

    role: RoleEnum
