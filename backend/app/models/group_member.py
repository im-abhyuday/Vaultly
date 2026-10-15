"""GroupMember association ORM model with composite foreign keys."""

import uuid

from sqlalchemy import ForeignKeyConstraint, Index
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class GroupMember(Base):
    """Membership of a user in a group with strict composite foreign keys."""

    __tablename__ = "group_members"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        nullable=False,
    )
    group_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        nullable=False,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        nullable=False,
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "group_id"],
            ["groups.tenant_id", "groups.id"],
            ondelete="CASCADE",
            name="fk_group_members_group",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "user_id"],
            ["memberships.tenant_id", "memberships.user_id"],
            ondelete="CASCADE",
            name="fk_group_members_membership",
        ),
        Index("ix_group_members_tenant_id", "tenant_id"),
        Index("ix_group_members_tenant_group", "tenant_id", "group_id"),
        Index("ix_group_members_tenant_user", "tenant_id", "user_id"),
    )
