"""Helpers to inspect database errors without leaking them to clients."""

from sqlalchemy.exc import IntegrityError

UNIQUE_VIOLATION = "23505"


def violated_constraint(exc: IntegrityError) -> str | None:
    """Name of the violated constraint/index (asyncpg exposes it on the original error)."""
    return getattr(exc.orig.__cause__, "constraint_name", None)


def is_unique_violation(exc: IntegrityError, constraint_name: str) -> bool:
    return (
        getattr(exc.orig, "sqlstate", None) == UNIQUE_VIOLATION
        and violated_constraint(exc) == constraint_name
    )
