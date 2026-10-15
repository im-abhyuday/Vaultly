"""Vaultly application configuration module.

Uses Pydantic v2 BaseSettings for type-safe environment variable parsing,
validation, and default values.
"""

from functools import lru_cache
from typing import Any

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Global configuration settings for the Vaultly platform."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
    )

    # General Application Settings
    PROJECT_NAME: str = "Vaultly"
    API_V1_STR: str = "/api/v1"
    ENVIRONMENT: str = "development"
    DEBUG: bool = True
    LOG_LEVEL: str = "INFO"
    ALLOWED_HOSTS: list[str] = ["*"]
    CORS_ORIGINS: list[str] = [
        "http://localhost:3000",
        "http://localhost:5173",
        "http://127.0.0.1:3000",
        "http://127.0.0.1:5173",
    ]

    # PostgreSQL Database
    POSTGRES_SERVER: str = "localhost"
    POSTGRES_PORT: int = 5432
    POSTGRES_USER: str = "tenantrag"
    POSTGRES_PASSWORD: str = "tenantrag_secret"
    POSTGRES_DB: str = "tenantrag"
    APP_DB_USER: str = "tenantrag_app"
    APP_DB_PASSWORD: str = "tenantrag_app_secret"
    MIGRATION_DATABASE_URL: str | None = None
    DATABASE_URL: str | None = None

    @field_validator("MIGRATION_DATABASE_URL", mode="before")
    @classmethod
    def assemble_migration_db_connection(cls, v: str | None, info: Any) -> str:
        """Construct migration owner database URL if not explicitly provided."""
        if isinstance(v, str) and v.strip():
            return v
        data = info.data
        user = data.get("POSTGRES_USER", "tenantrag")
        password = data.get("POSTGRES_PASSWORD", "tenantrag_secret")
        server = data.get("POSTGRES_SERVER", "localhost")
        port = data.get("POSTGRES_PORT", 5432)
        db = data.get("POSTGRES_DB", "tenantrag")
        return f"postgresql+asyncpg://{user}:{password}@{server}:{port}/{db}"

    @field_validator("DATABASE_URL", mode="before")
    @classmethod
    def assemble_db_connection(cls, v: str | None, info: Any) -> str:
        """Construct runtime database URL if not explicitly provided."""
        if isinstance(v, str) and v.strip():
            return v
        data = info.data
        user = data.get("APP_DB_USER", "tenantrag_app")
        password = data.get("APP_DB_PASSWORD", "tenantrag_app_secret")
        server = data.get("POSTGRES_SERVER", "localhost")
        port = data.get("POSTGRES_PORT", 5432)
        db = data.get("POSTGRES_DB", "tenantrag")
        return f"postgresql+asyncpg://{user}:{password}@{server}:{port}/{db}"

    # Redis (Semantic Cache & Rate Limiting)
    REDIS_URL: str = "redis://localhost:6379/0"
    REDIS_CACHE_TTL_SECONDS: int = 86400  # 24 hours
    REDIS_SIMILARITY_THRESHOLD: float = 0.95

    # Kafka / Redpanda Message Broker
    KAFKA_BOOTSTRAP_SERVERS: str = "localhost:19092"
    KAFKA_CONSUMER_GROUP_PREFIX: str = "tenantrag"

    # Authentication & Security
    JWT_SECRET_KEY: str = Field(
        default="dev_secret_key_change_me_in_production_min_32_bytes_long!",
        min_length=32,
    )
    JWT_ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 15
    REFRESH_TOKEN_EXPIRE_DAYS: int = 7

    # Embeddings & Vector Search
    EMBEDDING_MODEL: str = "BAAI/bge-small-en-v1.5"
    EMBEDDING_DIMENSION: int = 384

    # Reranker
    RERANKER_MODEL: str = "BAAI/bge-reranker-base"
    USE_RERANKER_DEFAULT: bool = True

    # LLM Settings (provider-agnostic: "ollama" or "openai")
    LLM_PROVIDER: str = "ollama"
    LLM_BASE_URL: str = "http://localhost:11434"
    LLM_API_KEY: str = "not-needed-for-ollama"
    LLM_MODEL: str = "llama3:8b"
    LLM_TEMPERATURE: float = 0.0

    # Ingestion & Chunking Defaults
    DEFAULT_CHUNK_SIZE: int = 512
    DEFAULT_CHUNK_OVERLAP: int = 64

    # Rate Limiting
    RATE_LIMIT_REQUESTS_PER_MINUTE: int = 60

    @property
    def is_production(self) -> bool:
        """Check if current environment is production."""
        return self.ENVIRONMENT.lower() == "production"

    @property
    def is_testing(self) -> bool:
        """Check if current environment is test."""
        return self.ENVIRONMENT.lower() == "test"


@lru_cache
def get_settings() -> Settings:
    """Return a cached instance of application settings."""
    return Settings()
