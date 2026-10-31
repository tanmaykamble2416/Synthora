import pytest
from pydantic import ValidationError

from app.core.config import Settings

VALID_SECRET = "a-valid-test-secret-that-is-long-enough-123456"


def _settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "postgres_user": "u",
        "postgres_password": "p",
        "postgres_db": "d",
        "jwt_secret_key": VALID_SECRET,
    }
    values.update(overrides)
    # _env_file=None: ignore any real .env so the test is deterministic.
    return Settings(_env_file=None, **values)


def test_database_url_escapes_special_characters_in_password() -> None:
    settings = _settings(postgres_password="p@ss:w/rd#1", postgres_port=5433)
    url = settings.database_url
    assert url.drivername == "postgresql+asyncpg"
    assert url.password == "p@ss:w/rd#1"
    assert url.port == 5433
    # The rendered string must escape '@', ':', '/', '#' so they are not parsed as URL syntax.
    rendered = url.render_as_string(hide_password=False)
    assert "p%40ss%3Aw%2Frd%231@" in rendered


def test_password_is_hidden_in_repr_and_url_string() -> None:
    settings = _settings(postgres_password="super-secret")
    assert "super-secret" not in repr(settings)
    assert "super-secret" not in str(settings.database_url)


def test_invalid_port_is_rejected() -> None:
    with pytest.raises(ValidationError):
        _settings(postgres_port=70000)


def test_missing_required_database_settings_are_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in ("POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_DB"):
        monkeypatch.delenv(key, raising=False)
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_docs_disabled_in_production() -> None:
    assert _settings(environment="development").docs_enabled is True
    assert _settings(environment="production").docs_enabled is False


# --- Day 2: JWT configuration -------------------------------------------------


def test_jwt_secret_is_required(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("JWT_SECRET_KEY", raising=False)
    with pytest.raises(ValidationError):
        Settings(_env_file=None, postgres_user="u", postgres_password="p", postgres_db="d")


def test_short_jwt_secret_is_rejected_without_echoing_it() -> None:
    with pytest.raises(ValidationError) as exc_info:
        _settings(jwt_secret_key="short-secret-xyz")
    assert "short-secret-xyz" not in str(exc_info.value)


def test_database_password_not_echoed_in_validation_errors() -> None:
    with pytest.raises(ValidationError) as exc_info:
        _settings(postgres_password="db-pass-very-secret", postgres_port=70000)
    assert "db-pass-very-secret" not in str(exc_info.value)


@pytest.mark.parametrize(("algorithm", "min_bytes"), [("HS256", 32), ("HS384", 48), ("HS512", 64)])
def test_secret_length_depends_on_algorithm(algorithm: str, min_bytes: int) -> None:
    _settings(jwt_algorithm=algorithm, jwt_secret_key="k" * min_bytes)
    with pytest.raises(ValidationError):
        _settings(jwt_algorithm=algorithm, jwt_secret_key="k" * (min_bytes - 1))


def test_placeholder_jwt_secret_is_rejected() -> None:
    with pytest.raises(ValidationError):
        _settings(jwt_secret_key="change-me-generate-a-real-secret-but-long-enough")


@pytest.mark.parametrize("algorithm", ["none", "RS256", "ES256", "HS1"])
def test_only_hmac_algorithms_are_configurable(algorithm: str) -> None:
    with pytest.raises(ValidationError):
        _settings(jwt_algorithm=algorithm)


@pytest.mark.parametrize("minutes", [0, 4, 1441])
def test_token_expiry_must_be_bounded(minutes: int) -> None:
    with pytest.raises(ValidationError):
        _settings(access_token_expire_minutes=minutes)


def test_jwt_secret_hidden_in_repr() -> None:
    assert VALID_SECRET not in repr(_settings())
    assert VALID_SECRET not in str(_settings().model_dump())


def test_database_url_for_other_database() -> None:
    url = _settings(postgres_db="dev").database_url_for("dev_test")
    assert url.database == "dev_test"
