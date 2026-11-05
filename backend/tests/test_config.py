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


# --- Day 6: GROBID and chunking configuration -----------------------------------


def test_grobid_is_disabled_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GROBID_ENABLED", raising=False)
    assert _settings().grobid_enabled is False


@pytest.mark.parametrize(
    ("raw", "normalized"),
    [
        ("http://127.0.0.1:8070", "http://127.0.0.1:8070"),
        ("http://grobid:8070/", "http://grobid:8070"),
        ("  https://grobid.internal  ", "https://grobid.internal"),
    ],
)
def test_grobid_url_is_normalized(raw: str, normalized: str) -> None:
    assert _settings(grobid_url=raw).grobid_url == normalized


@pytest.mark.parametrize(
    "url",
    [
        "ftp://127.0.0.1:8070",
        "file:///etc/passwd",
        "127.0.0.1:8070",
        "http://user:secret@127.0.0.1:8070",
        "http://127.0.0.1:8070/api/processFulltextDocument",
        "http://127.0.0.1:8070?x=1",
        "http://127.0.0.1:99999",
        "http://",
    ],
)
def test_unsafe_or_malformed_grobid_url_is_rejected(url: str) -> None:
    with pytest.raises(ValidationError) as excinfo:
        _settings(grobid_url=url)
    assert "secret" not in str(excinfo.value)


@pytest.mark.parametrize("seconds", [0, 4, 601])
def test_grobid_timeout_is_bounded(seconds: int) -> None:
    with pytest.raises(ValidationError):
        _settings(grobid_timeout_seconds=seconds)


def test_default_chunk_sizes() -> None:
    settings = _settings()
    assert (settings.chunk_min_words, settings.chunk_target_words, settings.chunk_max_words) == (60, 350, 500)


@pytest.mark.parametrize(
    ("minimum", "target", "maximum"),
    [(350, 350, 500), (60, 600, 500), (400, 350, 500)],
)
def test_inconsistent_chunk_sizes_are_rejected(minimum: int, target: int, maximum: int) -> None:
    with pytest.raises(ValidationError):
        _settings(chunk_min_words=minimum, chunk_target_words=target, chunk_max_words=maximum)


# --- Day 6 quality pass ---------------------------------------------------------------------


def test_quality_pass_defaults() -> None:
    settings = _settings()
    assert settings.extraction_detect_tables is True
    assert settings.chunk_token_counter == "word_proxy"
    assert settings.chunk_tokenizer_file is None
    assert settings.chunk_tokenizer_name == "BAAI/bge-small-en-v1.5"


@pytest.mark.parametrize("value", ["bert", "tokens", ""])
def test_unknown_token_counter_is_rejected(value: str) -> None:
    with pytest.raises(ValidationError):
        _settings(chunk_token_counter=value)


@pytest.mark.parametrize("name", ["has space", "x" * 51, "semi;colon", ""])
def test_tokenizer_label_is_restricted(name: str) -> None:
    with pytest.raises(ValidationError):
        _settings(chunk_tokenizer_name=name)
