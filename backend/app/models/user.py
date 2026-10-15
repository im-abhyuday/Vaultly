"""User ORM model."""

from datetime import UTC, datetime

from sqlalchemy import Boolean, DateTime, Index, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, UUIDPrimaryKeyMixin


class User(Base, UUIDPrimaryKeyMixin):
    """Global user identity table.

    No tenant_id and no Row-Level Security. App code accesses users
    exclusively via membership joins.
    """

    __tablename__ = "users"

    email: Mapped[str] = mapped_column(String(255), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    full_name: Mapped[str] = mapped_column(String(255), nullable=False)
    is_active: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        nullable=False,
        server_default="true",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        nullable=False,
        server_default=func.now(),
    )

    __table_args__ = (
        Index("uq_users_lower_email", func.lower(email), unique=True),
    )
