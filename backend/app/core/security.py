"""Password hashing (argon2id) and JWT access tokens.

Nothing in this module logs secrets, passwords, hashes or tokens.
"""

import uuid
from datetime import UTC, datetime, timedelta
from functools import lru_cache

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

from app.core.config import Settings
from app.core.exceptions import AuthenticationError

ACCESS_TOKEN_TYPE = "access"
REQUIRED_CLAIMS = ["sub", "exp", "iat", "nbf", "iss", "aud", "type"]
INVALID_TOKEN_MESSAGE = "Could not validate credentials."

# argon2-cffi's PasswordHasher defaults to Argon2id with RFC 9106 "low memory" parameters.
_password_hasher = PasswordHasher()


def hash_password(password: str) -> str:
    return _password_hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return _password_hasher.verify(password_hash, password)
    except (VerificationError, InvalidHashError):
        return False


def password_needs_rehash(password_hash: str) -> bool:
    """True when hashing parameters have been strengthened since this hash was made."""
    return _password_hasher.check_needs_rehash(password_hash)


@lru_cache
def _dummy_hash() -> str:
    return _password_hasher.hash("timing-equaliser-not-a-real-password")


def burn_password_verification_time(password: str) -> None:
    """Run a real argon2 verify for unknown emails so login timing does not reveal
    whether an account exists."""
    verify_password(_dummy_hash(), password)


def create_access_token(subject: uuid.UUID, settings: Settings) -> tuple[str, int]:
    """Return (token, expires_in_seconds)."""
    now = datetime.now(UTC)
    expires_in = settings.access_token_expire_minutes * 60
    payload = {
        "sub": str(subject),
        "type": ACCESS_TOKEN_TYPE,
        "iss": settings.jwt_issuer,
        "aud": settings.jwt_audience,
        "iat": now,
        "nbf": now,
        "exp": now + timedelta(seconds=expires_in),
        "jti": uuid.uuid4().hex,
    }
    token = jwt.encode(
        payload, settings.jwt_secret_key.get_secret_value(), algorithm=settings.jwt_algorithm
    )
    return token, expires_in


def decode_access_token(token: str, settings: Settings) -> uuid.UUID:
    """Validate signature, algorithm, required claims, issuer, audience, expiry,
    token type and that `sub` is a UUID. Any failure -> AuthenticationError (401)."""
    try:
        payload = jwt.decode(
            token,
            settings.jwt_secret_key.get_secret_value(),
            # Explicit single-algorithm allow-list: rejects "none", algorithm
            # confusion and tokens signed with any other HMAC variant.
            algorithms=[settings.jwt_algorithm],
            audience=settings.jwt_audience,
            issuer=settings.jwt_issuer,
            options={"require": REQUIRED_CLAIMS},
        )
    except jwt.PyJWTError as exc:
        raise AuthenticationError(INVALID_TOKEN_MESSAGE) from exc

    if payload.get("type") != ACCESS_TOKEN_TYPE:
        raise AuthenticationError(INVALID_TOKEN_MESSAGE)
    try:
        return uuid.UUID(str(payload["sub"]))
    except ValueError as exc:
        raise AuthenticationError(INVALID_TOKEN_MESSAGE) from exc
