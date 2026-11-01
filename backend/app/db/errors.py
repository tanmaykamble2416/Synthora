"""Helpers to inspect database errors without leaking them to clients."""

from sqlalchemy.exc import IntegrityError

UNIQUE_VIOLATION = "23505"
FOREIGN_KEY_VIOLATION = "23503"


def violated_constraint(exc: IntegrityError) -> str | None:
    """Name of the violated constraint/index (asyncpg exposes it on the original error)."""
    return getattr(exc.orig.__cause__, "constraint_name", None)


def _is_violation(exc: IntegrityError, sqlstate: str, constraint_name: str) -> bool:
    return getattr(exc.orig, "sqlstate", None) == sqlstate and violated_constraint(exc) == constraint_name


def is_unique_violation(exc: IntegrityError, constraint_name: str) -> bool:
    return _is_violation(exc, UNIQUE_VIOLATION, constraint_name)


def is_foreign_key_violation(exc: IntegrityError, constraint_name: str) -> bool:
    return _is_violation(exc, FOREIGN_KEY_VIOLATION, constraint_name)
