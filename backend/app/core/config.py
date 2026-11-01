"""Application configuration loaded from environment variables / the root .env file."""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import URL

# researchmind/backend/app/core/config.py -> parents[3] == researchmind/
PROJECT_ROOT = Path(__file__).resolve().parents[3]
ENV_FILE = PROJECT_ROOT / ".env"

JWT_SECRET_PLACEHOLDER_PREFIX = "change-me"
# RFC 7518 §3.2: an HMAC key must be at least as long as the hash output.
JWT_SECRET_MIN_BYTES = {"HS256": 32, "HS384": 48, "HS512": 64}


class Settings(BaseSettings):
    """Typed settings. Real environment variables override values in .env."""

    model_config = SettingsConfigDict(
        env_file=ENV_FILE,
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
        # Never echo raw values (passwords, JWT secret) inside validation errors/tracebacks.
        hide_input_in_errors=True,
    )

    # --- Application ---
    app_name: str = "SYNTHORA"
    environment: Literal["development", "test", "production"] = "development"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    api_v1_prefix: str = "/api/v1"

    # --- PostgreSQL ---
    postgres_host: str = "localhost"
    postgres_port: int = Field(default=5433, ge=1, le=65535)
    postgres_user: str = Field(min_length=1)
    postgres_password: SecretStr
    postgres_db: str = Field(min_length=1)

    # --- Connection pool ---
    db_pool_size: int = Field(default=5, ge=1, le=50)
    db_max_overflow: int = Field(default=10, ge=0, le=100)
    db_pool_timeout: int = Field(default=30, ge=1, le=300)
    db_connect_timeout: int = Field(default=5, ge=1, le=60)
    db_echo: bool = False

    # --- JWT authentication ---
    # No default: the app must not start with an implicit/weak signing key.
    jwt_secret_key: SecretStr
    # Restricted to HMAC algorithms. "none" and asymmetric algorithms are impossible
    # to configure, and decoding always passes this single algorithm explicitly.
    jwt_algorithm: Literal["HS256", "HS384", "HS512"] = "HS256"
    jwt_issuer: str = Field(default="synthora-api", min_length=1)
    jwt_audience: str = Field(default="synthora-clients", min_length=1)
    access_token_expire_minutes: int = Field(default=30, ge=5, le=1440)

    # --- Paper file storage ---
    # Relative paths are resolved against the PROJECT ROOT (not the current working
    # directory), so uvicorn/pytest started from backend/ use the same location.
    paper_storage_dir: Path = Path("storage")
    # Mebibytes (1 MB = 1024 * 1024 bytes).
    max_upload_size_mb: int = Field(default=25, ge=1, le=512)

    @field_validator("jwt_secret_key")
    @classmethod
    def _reject_placeholder_secret(cls, value: SecretStr) -> SecretStr:
        # Error messages deliberately never include the secret itself.
        if value.get_secret_value().strip().lower().startswith(JWT_SECRET_PLACEHOLDER_PREFIX):
            raise ValueError("JWT_SECRET_KEY is still the placeholder from .env.example; generate a real secret.")
        return value

    @model_validator(mode="after")
    def _check_secret_length_for_algorithm(self) -> "Settings":
        minimum = JWT_SECRET_MIN_BYTES[self.jwt_algorithm]
        if len(self.jwt_secret_key.get_secret_value().encode("utf-8")) < minimum:
            raise ValueError(f"JWT_SECRET_KEY must be at least {minimum} bytes for {self.jwt_algorithm}.")
        return self

    @property
    def database_url(self) -> URL:
        """Build the async DSN with URL.create so special characters in the
        password are escaped correctly, and the password never appears in repr()."""
        return self.database_url_for(self.postgres_db)

    def database_url_for(self, database: str) -> URL:
        """Same server and credentials, different database (used for the test DB)."""
        return URL.create(
            drivername="postgresql+asyncpg",
            username=self.postgres_user,
            password=self.postgres_password.get_secret_value(),
            host=self.postgres_host,
            port=self.postgres_port,
            database=database,
        )

    @property
    def docs_enabled(self) -> bool:
        return self.environment != "production"

    @property
    def paper_storage_root(self) -> Path:
        root = self.paper_storage_dir.expanduser()
        if not root.is_absolute():
            root = PROJECT_ROOT / root
        return root.resolve()

    @property
    def max_upload_bytes(self) -> int:
        return self.max_upload_size_mb * 1024 * 1024


@lru_cache
def get_settings() -> Settings:
    """Cached so .env is parsed once. Tests call get_settings.cache_clear()."""
    return Settings()
