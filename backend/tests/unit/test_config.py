"""Unit tests for configuration management (Pydantic Settings)."""

import pytest
from pydantic import ValidationError

from app.core.config import Settings


def test_default_settings_instantiation() -> None:
    """Verify settings instantiate with valid default values."""
    settings = Settings(
        JWT_SECRET_KEY="a" * 32,
        ENVIRONMENT="development",
    )
    assert settings.PROJECT_NAME == "Vaultly"
    assert settings.API_V1_STR == "/api/v1"
    assert settings.EMBEDDING_DIMENSION == 384
    assert settings.REDIS_SIMILARITY_THRESHOLD == 0.95
    assert not settings.is_production
    assert not settings.is_testing


def test_environment_flags() -> None:
    """Verify environment boolean helper flags."""
    prod_settings = Settings(
        JWT_SECRET_KEY="a" * 32,
        ENVIRONMENT="production",
    )
    assert prod_settings.is_production
    assert not prod_settings.is_testing

    test_settings = Settings(
        JWT_SECRET_KEY="a" * 32,
        ENVIRONMENT="test",
    )
    assert not test_settings.is_production
    assert test_settings.is_testing


def test_jwt_secret_key_minimum_length() -> None:
    """Ensure validation fails if JWT_SECRET_KEY is shorter than 32 characters."""
    with pytest.raises(ValidationError) as exc_info:
        Settings(JWT_SECRET_KEY="too-short")
    assert "JWT_SECRET_KEY" in str(exc_info.value)


def test_database_url_assembly() -> None:
    """Ensure database URLs are auto-assembled when not explicitly specified."""
    settings = Settings(
        JWT_SECRET_KEY="a" * 32,
        POSTGRES_USER="custom_owner",
        POSTGRES_PASSWORD="custom_owner_password",
        APP_DB_USER="custom_app",
        APP_DB_PASSWORD="custom_app_password",
        POSTGRES_SERVER="db.example.com",
        POSTGRES_PORT=5433,
        POSTGRES_DB="custom_db",
        MIGRATION_DATABASE_URL=None,
        DATABASE_URL=None,
    )
    assert (
        settings.MIGRATION_DATABASE_URL
        == "postgresql+asyncpg://custom_owner:custom_owner_password@db.example.com:5433/custom_db"
    )
    assert (
        settings.DATABASE_URL
        == "postgresql+asyncpg://custom_app:custom_app_password@db.example.com:5433/custom_db"
    )

