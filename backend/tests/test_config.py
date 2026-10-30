import pytest
from pydantic import ValidationError

from app.core.config import Settings


def _settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "postgres_user": "u",
        "postgres_password": "p",
        "postgres_db": "d",
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
