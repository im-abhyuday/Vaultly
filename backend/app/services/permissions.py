"""Document access control, query clause builders, and corpus change notifications."""

import uuid
from typing import Protocol

from sqlalchemy import and_, exists, literal, or_, select
from sqlalchemy.sql.elements import ColumnElement

from app.api.deps import CurrentUser
from app.models.document import Document
from app.models.document_acl import DocumentACL
from app.models.enums import DocumentVisibility, RoleEnum
from app.models.group_member import GroupMember


def content_accessible(user: CurrentUser) -> ColumnElement[bool]:
    """Build SQLAlchemy filter for documents whose content can be read by the user.

    Rule:
    - Owner can always access.
    - TENANT visibility: Any active tenant member can access.
    - GROUP visibility: Accessible if user_id is in document_acl OR user belongs to a group in document_acl.
    - PRIVATE visibility: Accessible by owner ONLY (Admins/Auditors do NOT bypass content access).
    """
    owner_clause = Document.owner_id == user.user_id
    tenant_clause = Document.visibility == DocumentVisibility.TENANT.value

    # Direct user ACL grant
    direct_user_acl = exists(
        select(1).where(
            DocumentACL.document_id == Document.id,
            DocumentACL.user_id == user.user_id,
        )
    )

    # Group ACL grant (user is member of granted group)
    group_acl = exists(
        select(1)
        .select_from(DocumentACL)
        .join(GroupMember, GroupMember.group_id == DocumentACL.group_id)
        .where(
            DocumentACL.document_id == Document.id,
            GroupMember.user_id == user.user_id,
        )
    )

    group_clause = and_(
        Document.visibility == DocumentVisibility.GROUP.value,
        or_(direct_user_acl, group_acl),
    )

    return or_(owner_clause, tenant_clause, group_clause)


def metadata_visible(user: CurrentUser) -> ColumnElement[bool]:
    """Build SQLAlchemy filter for documents whose metadata can be viewed by the user.

    Rule:
    - ADMIN and AUDITOR roles can view metadata of all documents within the tenant.
    - Other users can view metadata if and only if they have content access.
    """
    if user.role in (RoleEnum.ADMIN, RoleEnum.AUDITOR):
        return literal(True)
    return content_accessible(user)


def can_manage(user: CurrentUser, doc: Document) -> bool:
    """Return True if user has administrative rights to manage or delete the document.

    Rule:
    - AUDITOR is strictly read-only everywhere.
    - ADMIN can manage any document in their tenant.
    - The document owner (with active MEMBER role) can manage their own document.
    """
    if user.role == RoleEnum.AUDITOR:
        return False
    if user.role == RoleEnum.ADMIN:
        return True
    return doc.owner_id == user.user_id and user.role == RoleEnum.MEMBER


class CorpusChangeNotifier(Protocol):
    """Protocol for notifying caches of tenant document corpus changes."""

    async def notify(self, tenant_id: uuid.UUID, reason: str) -> None:
        """Emit corpus modification notification."""
        ...


class NoOpCorpusChangeNotifier:
    """Default no-op implementation of CorpusChangeNotifier (Redis cache will hook in Phase 4)."""

    async def notify(self, tenant_id: uuid.UUID, reason: str) -> None:
        """No-op."""
        pass


_notifier_instance: CorpusChangeNotifier = NoOpCorpusChangeNotifier()


def get_corpus_notifier() -> CorpusChangeNotifier:
    """Return the active corpus change notifier."""
    return _notifier_instance


def set_corpus_notifier(notifier: CorpusChangeNotifier) -> None:
    """Set custom corpus change notifier (e.g. for testing or Phase 4 Redis hook)."""
    global _notifier_instance
    _notifier_instance = notifier
