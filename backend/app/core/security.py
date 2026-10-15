"""Cryptographic and security primitives for Vaultly.

Handles Argon2id password hashing with timing-attack mitigation,
HMAC-SHA256 JWT access token encoding/validation, opaque refresh token hashing,
and Redis token denylisting.
"""

import hashlib
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
import redis.asyncio as aioredis
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError

from app.core.config import get_settings

settings = get_settings()

# Initialize Argon2id hasher with secure parameters
_pwd_hasher = PasswordHasher(
    time_cost=3,
    memory_cost=65536,  # 64 MB
    parallelism=4,
    hash_len=32,
    salt_len=16,
)

# Dummy hash for timing attack mitigation during failed user lookup
DUMMY_PASSWORD_HASH = _pwd_hasher.hash("dummy_timing_mitigation_password_12345")


def verify_jwt_secret() -> None:
    """Refuse to start outside development if JWT_SECRET_KEY is default or too short."""
    if settings.ENVIRONMENT.lower() not in ("development", "test"):
        if (
            len(settings.JWT_SECRET_KEY) < 32
            or "change_me" in settings.JWT_SECRET_KEY.lower()
            or "secret_key" in settings.JWT_SECRET_KEY.lower()
        ):
            raise RuntimeError(
                "Insecure configuration: JWT_SECRET_KEY must be >= 32 chars and not use default/dev secrets in production."
            )


verify_jwt_secret()


def hash_password(password: str) -> str:
    """Hash plaintext password using Argon2id."""
    if len(password) < 12:
        raise ValueError("Password must be at least 12 characters long.")
    if len(password) > 128:
        raise ValueError("Password cannot exceed 128 characters.")
    return _pwd_hasher.hash(password)


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Verify password against Argon2id hash, returning boolean."""
    try:
        return _pwd_hasher.verify(hashed_password, plain_password)
    except (VerifyMismatchError, Exception):
        return False


def verify_dummy_password(plain_password: str) -> bool:
    """Run dummy Argon2id hash verification to ensure uniform response timing."""
    verify_password(plain_password, DUMMY_PASSWORD_HASH)
    return False


def create_access_token(
    user_id: uuid.UUID,
    tenant_id: uuid.UUID,
    expires_delta: timedelta | None = None,
) -> tuple[str, str, int]:
    """Generate HS256 JWT access token.

    Returns:
        (token_str, jti, exp_timestamp)
    """
    now = datetime.now(UTC)
    delta = expires_delta or timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    exp = now + delta
    jti = str(uuid.uuid4())

    payload: dict[str, Any] = {
        "sub": str(user_id),
        "tid": str(tenant_id),
        "jti": jti,
        "type": "access",
        "iat": int(now.timestamp()),
        "exp": int(exp.timestamp()),
        "iss": "vaultly",
        "aud": "vaultly-api",
    }

    token = jwt.encode(payload, settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM)
    return token, jti, int(exp.timestamp())


def decode_access_token(token: str) -> dict[str, Any]:
    """Decode and strictly validate JWT access token claims."""
    try:
        # Strictly verify signature, algorithms, exp, iss, and aud
        payload = jwt.decode(
            token,
            settings.JWT_SECRET_KEY,
            algorithms=[settings.JWT_ALGORITHM],
            audience="vaultly-api",
            issuer="vaultly",
            options={
                "verify_signature": True,
                "verify_exp": True,
                "verify_iat": True,
                "verify_aud": True,
                "verify_iss": True,
                "require": ["sub", "tid", "jti", "type", "exp", "iat", "iss", "aud"],
            },
        )
    except jwt.PyJWTError as e:
        raise ValueError(f"Invalid access token: {e}") from e

    if payload.get("type") != "access":
        raise ValueError("Invalid token type: expected 'access'")

    return payload


def generate_opaque_token(tenant_id: uuid.UUID) -> tuple[str, str]:
    """Generate prefixed opaque token: <tenant_uuid>.<random_secret>.

    Returns:
        (full_token, token_hash_sha256)
    """
    secret = secrets.token_urlsafe(32)
    full_token = f"{tenant_id}.{secret}"
    token_hash = hash_token_secret(full_token)
    return full_token, token_hash


def hash_token_secret(token: str) -> str:
    """Compute deterministic SHA-256 hash of opaque token string."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def parse_prefixed_token(token: str) -> tuple[uuid.UUID, str]:
    """Parse <tenant_uuid>.<secret> format and extract tenant_id."""
    parts = token.strip().split(".", 1)
    if len(parts) != 2:
        raise ValueError("Invalid token format. Expected '<tenant_uuid>.<secret>'")
    try:
        tenant_id = uuid.UUID(parts[0])
    except ValueError as e:
        raise ValueError(f"Invalid tenant UUID prefix in token: {e}") from e
    return tenant_id, parts[1]


# ------------------------------------------------------------------------------
# Redis Access Token Denylist
# ------------------------------------------------------------------------------

async def denylist_access_token(jti: str, exp_timestamp: int) -> None:
    """Store revoked access token JTI in Redis until its expiration time."""
    now = int(datetime.now(UTC).timestamp())
    ttl = exp_timestamp - now
    if ttl <= 0:
        return

    try:
        client = aioredis.from_url(str(settings.REDIS_URL), decode_responses=True)
        await client.setex(f"denylist:jti:{jti}", ttl, "revoked")
        await client.close()
    except Exception:
        # Fails open in development / test if Redis unreachable; documented in DECISIONS.md
        pass


async def is_token_denylisted(jti: str) -> bool:
    """Check if access token JTI is in Redis denylist.

    Returns False (fails open) if Redis is unavailable, documenting the trade-off.
    """
    try:
        client = aioredis.from_url(str(settings.REDIS_URL), decode_responses=True)
        val = await client.get(f"denylist:jti:{jti}")
        await client.close()
        return val is not None
    except Exception:
        # Fail open: availability favored over immediate invalidation if cache layer is degraded
        return False
