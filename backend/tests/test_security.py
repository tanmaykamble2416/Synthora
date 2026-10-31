"""Unit tests for password hashing and JWT validation (no database)."""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
import pytest

from app.core.config import Settings
from app.core.exceptions import AuthenticationError
from app.core.security import (
    create_access_token,
    decode_access_token,
    hash_password,
    password_needs_rehash,
    verify_password,
)

# 64+ bytes so forging test tokens with HS384/HS512 uses a properly sized key too.
SECRET = "security-test-secret-key-0123456789-abcdefghijklmnopqrstuvwxyz-0123"


@pytest.fixture
def settings() -> Settings:
    return Settings(
        _env_file=None,
        postgres_user="u",
        postgres_password="p",
        postgres_db="d",
        jwt_secret_key=SECRET,
        jwt_algorithm="HS256",
        jwt_issuer="synthora-api",
        jwt_audience="synthora-clients",
        access_token_expire_minutes=30,
    )


def _claims(**overrides: Any) -> dict[str, Any]:
    now = datetime.now(UTC)
    claims: dict[str, Any] = {
        "sub": str(uuid.uuid4()),
        "type": "access",
        "iss": "synthora-api",
        "aud": "synthora-clients",
        "iat": now,
        "nbf": now,
        "exp": now + timedelta(minutes=5),
    }
    claims.update(overrides)
    return {k: v for k, v in claims.items() if v is not None}


def _encode(claims: dict[str, Any], key: str = SECRET, algorithm: str = "HS256") -> str:
    return jwt.encode(claims, key, algorithm=algorithm)


# --- passwords -------------------------------------------------------------------


def test_password_hash_is_argon2id_and_verifies() -> None:
    hashed = hash_password("correct horse battery")
    assert hashed.startswith("$argon2id$")
    assert "correct horse battery" not in hashed
    assert verify_password(hashed, "correct horse battery")
    assert not verify_password(hashed, "wrong password")
    assert not password_needs_rehash(hashed)


def test_verify_password_handles_garbage_hash() -> None:
    assert verify_password("not-a-hash", "anything") is False


def test_same_password_hashes_differently() -> None:
    assert hash_password("same-password") != hash_password("same-password")


# --- tokens ----------------------------------------------------------------------


def test_token_round_trip_returns_uuid_subject(settings: Settings) -> None:
    user_id = uuid.uuid4()
    token, expires_in = create_access_token(user_id, settings)
    assert expires_in == 30 * 60
    assert decode_access_token(token, settings) == user_id


def test_token_header_uses_configured_algorithm(settings: Settings) -> None:
    token, _ = create_access_token(uuid.uuid4(), settings)
    assert jwt.get_unverified_header(token)["alg"] == "HS256"


def test_token_does_not_contain_secret(settings: Settings) -> None:
    token, _ = create_access_token(uuid.uuid4(), settings)
    assert SECRET not in token


@pytest.mark.parametrize(
    "claims",
    [
        pytest.param(_claims(exp=datetime.now(UTC) - timedelta(seconds=1)), id="expired"),
        pytest.param(_claims(nbf=datetime.now(UTC) + timedelta(minutes=10)), id="not-yet-valid"),
        pytest.param(_claims(iss="someone-else"), id="wrong-issuer"),
        pytest.param(_claims(aud="other-app"), id="wrong-audience"),
        pytest.param(_claims(type="refresh"), id="wrong-type"),
        pytest.param(_claims(sub="not-a-uuid"), id="sub-not-uuid"),
        pytest.param(_claims(sub="12345"), id="sub-numeric-string"),
        pytest.param(_claims(exp=None), id="missing-exp"),
        pytest.param(_claims(iat=None), id="missing-iat"),
        pytest.param(_claims(nbf=None), id="missing-nbf"),
        pytest.param(_claims(sub=None), id="missing-sub"),
        pytest.param(_claims(iss=None), id="missing-iss"),
        pytest.param(_claims(aud=None), id="missing-aud"),
        pytest.param(_claims(type=None), id="missing-type"),
    ],
)
def test_invalid_claims_are_rejected(settings: Settings, claims: dict[str, Any]) -> None:
    with pytest.raises(AuthenticationError):
        decode_access_token(_encode(claims), settings)


def test_valid_hand_built_token_is_accepted(settings: Settings) -> None:
    claims = _claims()
    assert decode_access_token(_encode(claims), settings) == uuid.UUID(claims["sub"])


def test_wrong_signing_key_is_rejected(settings: Settings) -> None:
    token = _encode(_claims(), key="another-secret-key-0123456789-abcdefghijklmnopqrstuvwxyz-012345")
    with pytest.raises(AuthenticationError):
        decode_access_token(token, settings)


@pytest.mark.parametrize("algorithm", ["HS384", "HS512"])
def test_other_hmac_algorithm_is_rejected(settings: Settings, algorithm: str) -> None:
    # Correct key, valid claims, but not the configured algorithm.
    with pytest.raises(AuthenticationError):
        decode_access_token(_encode(_claims(), algorithm=algorithm), settings)


def test_alg_none_token_is_rejected(settings: Settings) -> None:
    unsigned = jwt.encode(_claims(), key=None, algorithm="none")
    with pytest.raises(AuthenticationError):
        decode_access_token(unsigned, settings)


def test_tampered_payload_is_rejected(settings: Settings) -> None:
    token, _ = create_access_token(uuid.uuid4(), settings)
    header, payload, signature = token.split(".")
    forged_payload = _encode(_claims()).split(".")[1]
    with pytest.raises(AuthenticationError):
        decode_access_token(f"{header}.{forged_payload}.{signature}", settings)


@pytest.mark.parametrize("garbage", ["", "abc", "a.b.c", "Bearer x.y.z"])
def test_malformed_tokens_are_rejected(settings: Settings, garbage: str) -> None:
    with pytest.raises(AuthenticationError):
        decode_access_token(garbage, settings)
