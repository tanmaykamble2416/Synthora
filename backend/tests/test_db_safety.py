import pytest

from tests.db_safety import UnsafeTestDatabaseError, assert_safe_test_database


def test_accepts_dedicated_test_database() -> None:
    assert_safe_test_database("researchmind_test", "researchmind")


@pytest.mark.parametrize(
    ("test_db", "dev_db"),
    [
        ("researchmind", "researchmind"),        # the dev DB itself
        ("researchmind_test", "researchmind_test"),  # dev configured with the same name
        ("researchmind_testing", "researchmind"),  # does not end with _test
        ("prod", "researchmind"),
        ('x_test"; DROP DATABASE researchmind; --', "researchmind"),  # injection attempt
        ("Researchmind_Test", "researchmind"),   # not a plain lower-case identifier
    ],
)
def test_rejects_unsafe_names(test_db: str, dev_db: str) -> None:
    with pytest.raises(UnsafeTestDatabaseError):
        assert_safe_test_database(test_db, dev_db)
