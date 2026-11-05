"""Application configuration loaded from environment variables / the root .env file."""

from functools import lru_cache
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

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
    # 127.0.0.1, not "localhost": docker-compose publishes PostgreSQL on IPv4 127.0.0.1 only.
    # On Windows "localhost" resolves to IPv6 ::1 first, and each refused ::1 attempt costs ~2 s
    # before falling back to 127.0.0.1.
    postgres_host: str = "127.0.0.1"
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

    # --- PDF text extraction (Day 5) ---
    # Documents with more pages are rejected (failure_code = too_many_pages).
    extraction_max_pages: int = Field(default=2000, ge=1, le=20_000)
    # A paper left in "processing" for longer than this (e.g. after a crash) may be
    # claimed again by a new extraction request.
    extraction_stale_minutes: int = Field(default=15, ge=1, le=1440)
    # Day 6 quality pass: find table regions with PyMuPDF so that GROBID paragraphs made of
    # table cells become separate table chunks. Runs only when GROBID's TEI is used (~0.1 s/page).
    extraction_detect_tables: bool = True

    # --- GROBID document structure (Day 6) ---
    # Optional. When disabled or unreachable, extraction still succeeds with the
    # PyMuPDF page-based fallback (structure_source = page_fallback).
    grobid_enabled: bool = False
    # Base URL only (scheme://host:port), no credentials, path, query or fragment.
    grobid_url: str = "http://127.0.0.1:8070"
    # Upper bound for one full-text request (large papers take tens of seconds on CPU).
    grobid_timeout_seconds: int = Field(default=120, ge=5, le=600)

    # --- Chunking (Day 6) ---
    # Sizes are in WORDS (whitespace-separated), a temporary proxy for tokens until
    # Day 7 introduces the embedding model's real tokenizer.
    chunk_target_words: int = Field(default=350, ge=50, le=2000)
    chunk_max_words: int = Field(default=500, ge=50, le=4000)
    chunk_min_words: int = Field(default=60, ge=1, le=1000)
    # What the three sizes above count (Day 6 quality pass). "word_proxy" (default) counts
    # words. "tokenizer" uses CHUNK_TOKENIZER_FILE (a local tokenizer.json, never downloaded)
    # and falls back to word_proxy if it or the optional `tokenizers` package is missing.
    chunk_token_counter: Literal["word_proxy", "tokenizer"] = "word_proxy"
    chunk_tokenizer_file: Path | None = None
    # Label stored as token_count_method = "tokenizer:<name>".
    chunk_tokenizer_name: str = Field(default="BAAI/bge-small-en-v1.5", pattern=r"^[A-Za-z0-9._@/\-]{1,50}$")

    @field_validator("grobid_url")
    @classmethod
    def _validate_grobid_url(cls, value: str) -> str:
        parts = urlsplit(value.strip())
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ValueError("GROBID_URL must be an http(s) URL such as http://127.0.0.1:8070.")
        if parts.username or parts.password:
            raise ValueError("GROBID_URL must not contain credentials.")
        if parts.path not in ("", "/") or parts.query or parts.fragment:
            raise ValueError("GROBID_URL must be a base URL without path, query or fragment.")
        try:
            parts.port  # noqa: B018 - raises ValueError for an out-of-range port
        except ValueError as exc:
            raise ValueError("GROBID_URL has an invalid port.") from exc
        return f"{parts.scheme}://{parts.netloc}"

    @model_validator(mode="after")
    def _check_chunk_sizes(self) -> "Settings":
        if not (self.chunk_min_words < self.chunk_target_words <= self.chunk_max_words):
            raise ValueError("Chunk sizes must satisfy CHUNK_MIN_WORDS < CHUNK_TARGET_WORDS <= CHUNK_MAX_WORDS.")
        return self

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
