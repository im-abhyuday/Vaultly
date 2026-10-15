"""Enumerations for RBAC and document processing."""

import enum


class RoleEnum(str, enum.Enum):
    """Supported multi-tenant RBAC roles."""

    ADMIN = "ADMIN"
    MEMBER = "MEMBER"
    AUDITOR = "AUDITOR"


class DocumentStatus(str, enum.Enum):
    """Document ingestion pipeline statuses."""

    UPLOADED = "UPLOADED"
    PARSING = "PARSING"
    PARSED = "PARSED"
    INDEXING = "INDEXING"
    READY = "READY"
    FAILED = "FAILED"


class DocumentErrorCode(str, enum.Enum):
    """Failure error codes for document processing."""

    UNSUPPORTED_TYPE = "UNSUPPORTED_TYPE"
    CORRUPT_FILE = "CORRUPT_FILE"
    NO_EXTRACTABLE_TEXT = "NO_EXTRACTABLE_TEXT"
    LIMIT_EXCEEDED = "LIMIT_EXCEEDED"
    RETRIES_EXHAUSTED = "RETRIES_EXHAUSTED"
    INTERNAL_ERROR = "INTERNAL_ERROR"


class DocumentVisibility(str, enum.Enum):
    """Document visibility and access control levels."""

    PRIVATE = "PRIVATE"
    GROUP = "GROUP"
    TENANT = "TENANT"
