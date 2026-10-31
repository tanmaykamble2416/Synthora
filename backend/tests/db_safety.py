"""Guard that makes it impossible for integration tests to target the development DB."""

import re

_IDENTIFIER = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")


class UnsafeTestDatabaseError(RuntimeError):
    pass


def assert_safe_test_database(test_db: str, dev_db: str) -> None:
    if not _IDENTIFIER.fullmatch(test_db):
        raise UnsafeTestDatabaseError(
            f"TEST_POSTGRES_DB={test_db!r} must be a plain lower-case identifier."
        )
    if not test_db.endswith("_test"):
        raise UnsafeTestDatabaseError(f"TEST_POSTGRES_DB={test_db!r} must end with '_test'.")
    if test_db == dev_db:
        raise UnsafeTestDatabaseError("TEST_POSTGRES_DB must differ from POSTGRES_DB.")
