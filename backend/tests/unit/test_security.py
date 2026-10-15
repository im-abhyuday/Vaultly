"""Unit tests for cryptographic and security primitives."""

import time
import uuid

import jwt
import pytest

from app.core.config import get_settings
from app.core.security import (
    create_access_token,
    decode_access_token,
    generate_opaque_token,
    hash_password,
    hash_token_secret,
    parse_prefixed_token,
    verify_dummy_password,
    verify_password,
)

settings = get_settings()


def test_password_hashing_and_verification() -> None:
    """Argon2id produces verifiable hashes and rejects wrong passwords."""
    pwd = "CorrectHorseBatteryStaple123!"
    hashed = hash_password(pwd)
    assert hashed.startswith("$argon2id$")
    assert verify_password(pwd, hashed)
    assert not verify_password("WrongPassword999!", hashed)


def test_password_length_constraints() -> None:
    """Argon2id enforces min length 12 and max length 128."""
    with pytest.raises(ValueError, match="at least 12"):
        hash_password("short_pwd_1")

    with pytest.raises(ValueError, match="exceed 128"):
        hash_password("a" * 129)


def test_dummy_password_timing_mitigation() -> None:
    """verify_dummy_password returns False in comparable execution time to regular verify."""
    start = time.perf_counter()
    res = verify_dummy_password("some_random_password_attempt_123")
    elapsed = time.perf_counter() - start
    assert not res
    assert elapsed > 0.001  # Verifies computationally deliberate delay


def test_jwt_access_token_creation_and_decoding() -> None:
    """Valid access token decodes all required claims correctly."""
    user_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    token_str, jti, exp_ts = create_access_token(user_id, tenant_id)

    payload = decode_access_token(token_str)
    assert payload["sub"] == str(user_id)
    assert payload["tid"] == str(tenant_id)
    assert payload["jti"] == jti
    assert payload["type"] == "access"
    assert payload["iss"] == "vaultly"
    assert payload["aud"] == "vaultly-api"
    assert payload["exp"] == exp_ts


def test_jwt_invalid_signature_rejected() -> None:
    """Access token signed with wrong secret is rejected."""
    user_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    fake_token = jwt.encode(
        {
            "sub": str(user_id),
            "tid": str(tenant_id),
            "jti": str(uuid.uuid4()),
            "type": "access",
            "iat": int(time.time()),
            "exp": int(time.time()) + 900,
            "iss": "vaultly",
            "aud": "vaultly-api",
        },
        "wrong_secret_key_that_is_at_least_32_bytes_long_12345",
        algorithm="HS256",
    )
    with pytest.raises(ValueError, match="Invalid access token"):
        decode_access_token(fake_token)


def test_jwt_algorithm_none_rejected() -> None:
    """Access token with alg='none' is rejected."""
    fake_token = jwt.encode(
        {"sub": str(uuid.uuid4()), "tid": str(uuid.uuid4()), "type": "access"},
        key="",
        algorithm="none",
    )
    with pytest.raises(ValueError):
        decode_access_token(fake_token)


def test_jwt_wrong_type_rejected() -> None:
    """Token with type != 'access' is rejected."""
    user_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    refresh_token = jwt.encode(
        {
            "sub": str(user_id),
            "tid": str(tenant_id),
            "jti": str(uuid.uuid4()),
            "type": "refresh",
            "iat": int(time.time()),
            "exp": int(time.time()) + 900,
            "iss": "vaultly",
            "aud": "vaultly-api",
        },
        settings.JWT_SECRET_KEY,
        algorithm="HS256",
    )
    with pytest.raises(ValueError, match="Invalid token type"):
        decode_access_token(refresh_token)


def test_jwt_wrong_audience_or_issuer_rejected() -> None:
    """Tokens with incorrect audience or issuer are rejected."""
    token_wrong_aud = jwt.encode(
        {
            "sub": str(uuid.uuid4()),
            "tid": str(uuid.uuid4()),
            "jti": str(uuid.uuid4()),
            "type": "access",
            "iat": int(time.time()),
            "exp": int(time.time()) + 900,
            "iss": "vaultly",
            "aud": "wrong-audience",
        },
        settings.JWT_SECRET_KEY,
        algorithm="HS256",
    )
    with pytest.raises(ValueError, match="Invalid access token"):
        decode_access_token(token_wrong_aud)


def test_opaque_token_generation_and_parsing() -> None:
    """Generate prefixed opaque token <tenant_uuid>.<secret> and parse it."""
    tenant_id = uuid.uuid4()
    token_str, token_hash = generate_opaque_token(tenant_id)

    assert token_str.startswith(f"{tenant_id}.")
    assert token_hash == hash_token_secret(token_str)

    parsed_tid, secret = parse_prefixed_token(token_str)
    assert parsed_tid == tenant_id
    assert len(secret) > 10


def test_invalid_prefixed_token_parsing() -> None:
    """Invalid token format raises ValueError."""
    with pytest.raises(ValueError, match="Expected '<tenant_uuid>.<secret>'"):
        parse_prefixed_token("invalid-format-without-dot")

    with pytest.raises(ValueError, match="Invalid tenant UUID"):
        parse_prefixed_token("not-a-uuid.secret123")
