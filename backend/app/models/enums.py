"""Role enumeration for RBAC."""

import enum


class RoleEnum(str, enum.Enum):
    """Supported multi-tenant RBAC roles."""

    ADMIN = "ADMIN"
    MEMBER = "MEMBER"
    AUDITOR = "AUDITOR"
