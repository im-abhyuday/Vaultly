"""Unit tests for SQLAlchemy base models and mixins."""

import uuid

from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class SampleModel(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Concrete model for testing declarative base and mixins."""

    __tablename__ = "sample_test_models"

    name: Mapped[str] = mapped_column()


def test_uuid_primary_key_mixin() -> None:
    """Verify UUIDPrimaryKeyMixin configures UUID primary key with default."""
    col = SampleModel.__table__.columns["id"]
    assert col.primary_key is True
    assert col.default is not None
    assert callable(col.default.arg)
    generated_id = col.default.arg(None)
    assert isinstance(generated_id, uuid.UUID)


def test_timestamp_mixin() -> None:
    """Verify TimestampMixin sets created_at and updated_at column defaults."""
    created_col = SampleModel.__table__.columns["created_at"]
    updated_col = SampleModel.__table__.columns["updated_at"]
    assert created_col.nullable is False
    assert created_col.default is not None
    assert updated_col.nullable is False
    assert updated_col.onupdate is not None
