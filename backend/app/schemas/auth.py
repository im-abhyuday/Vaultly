"""Pydantic validation schemas for authentication and registration."""

import uuid

from pydantic import BaseModel, ConfigDict, Field

# Slug regex constraint: ^[a-z0-9][a-z0-9-]{1,38}[a-z0-9]$
SLUG_REGEX = r"^[a-z0-9][a-z0-9-]{1,38}[a-z0-9]$"
EMAIL_REGEX = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"


class RegisterTenantRequest(BaseModel):
    """Payload to register a new tenant and its initial admin user."""

    tenant_name: str = Field(min_length=2, max_length=255)
    tenant_slug: str = Field(pattern=SLUG_REGEX, min_length=3, max_length=40)
    email: str = Field(pattern=EMAIL_REGEX, min_length=5, max_length=255)
    password: str = Field(min_length=12, max_length=128)
    full_name: str = Field(min_length=1, max_length=255)


class TokenResponse(BaseModel):
    """Standard access and refresh token response."""

    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int


class RefreshTokenRequest(BaseModel):
    """Payload to refresh an access token using an opaque refresh token."""

    refresh_token: str


class AcceptInviteRequest(BaseModel):
    """Payload to accept an invitation and complete account setup."""

    token: str
    password: str = Field(min_length=12, max_length=128)
    full_name: str = Field(min_length=1, max_length=255)


class UserProfileResponse(BaseModel):
    """Profile of the currently authenticated user in the active tenant."""

    model_config = ConfigDict(from_attributes=True)

    user_id: uuid.UUID
    email: str
    full_name: str
    tenant_id: uuid.UUID
    role: str
    tenant_name: str
    tenant_slug: str
