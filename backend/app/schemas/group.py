"""Pydantic validation schemas for groups and group memberships."""

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class GroupCreateRequest(BaseModel):
    """Payload to create a new group."""

    name: str = Field(min_length=1, max_length=128)
    description: str | None = Field(default=None, max_length=1000)


class GroupUpdateRequest(BaseModel):
    """Payload to update an existing group."""

    name: str | None = Field(default=None, min_length=1, max_length=128)
    description: str | None = Field(default=None, max_length=1000)


class GroupResponse(BaseModel):
    """Group details response."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    tenant_id: uuid.UUID
    name: str
    description: str | None
    created_at: datetime


class AddGroupMemberRequest(BaseModel):
    """Payload to add a user to a group."""

    user_id: uuid.UUID


class GroupMemberResponse(BaseModel):
    """Details of a user belonging to a group."""

    model_config = ConfigDict(from_attributes=True)

    user_id: uuid.UUID
    email: str
    full_name: str
    role: str
