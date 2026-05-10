"""Application settings loaded from environment variables (12-Factor: Config).

Per the 12-Factor App methodology, every configuration value that varies between
deploys (database URL, secret key, CORS origins, etc.) lives in the environment.
We use Pydantic's `BaseSettings` so values are parsed, validated, and typed once
at startup — failing fast if anything is missing or malformed.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, PostgresDsn, RedisDsn, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Strongly-typed application settings.

    Values are read from (in order of precedence):
      1. Real OS environment variables.
      2. A local `.env` file at repository root (development only).

    `.env` is gitignored; `.env.example` is committed as a template.
    """

    model_config = SettingsConfigDict(
        # Try the local cwd first (process started in repo root), then walk up
        # one level (process started in `backend/`). In Docker neither file
        # exists and real env vars take over.
        env_file=(".env", "../.env"),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- Application metadata -------------------------------------------------
    APP_NAME: str = "smart-order-backend"
    APP_ENV: Literal["dev", "staging", "prod", "test"] = "dev"
    APP_VERSION: str = "0.1.0"
    DEBUG: bool = False

    # --- API ------------------------------------------------------------------
    API_V1_PREFIX: str = "/api/v1"
    # Comma-separated origins; parsed into a list below.
    CORS_ORIGINS: str = "http://localhost:5173"

    # --- Database -------------------------------------------------------------
    DATABASE_URL: PostgresDsn = Field(
        default=...,  # required — fail fast if missing
        description="SQLAlchemy DSN, e.g. postgresql+psycopg://user:pass@host:5432/db",
    )
    DB_POOL_SIZE: int = 5
    DB_MAX_OVERFLOW: int = 10
    DB_POOL_PRE_PING: bool = True

    # --- Redis / Celery -------------------------------------------------------
    REDIS_URL: RedisDsn = Field(default=..., description="redis://host:6379/0")
    CELERY_BROKER_URL: RedisDsn | None = None
    CELERY_RESULT_BACKEND: RedisDsn | None = None

    # --- Security -------------------------------------------------------------
    JWT_SECRET: SecretStr = Field(default=..., description="HMAC secret for JWT signing")
    JWT_ALGORITHM: Literal["HS256", "HS384", "HS512"] = "HS256"
    JWT_ACCESS_TOKEN_TTL_SECONDS: int = 60 * 60  # 1 hour

    # --- Scheduler tuning -----------------------------------------------------
    # Business / operational knobs that callers may want to change per
    # environment. Algorithm-internal magic numbers (e.g. score-encoding
    # offsets) stay as code constants.
    SCHEDULER_DAILY_CAPACITY: int = Field(
        default=10_000,
        gt=0,
        description=("Wafers per day; segment trees use this as the per-day capacity."),
    )
    SCHEDULER_HORIZON_DAYS: int = Field(
        default=30,
        gt=0,
        description=(
            "How many days ahead the scheduler accepts deadlines for; "
            "segment trees are sized by this."
        ),
    )
    SCHEDULER_RUN_WAIT_TIMEOUT_SECONDS: int = Field(
        default=5 * 60,
        gt=0,
        description=(
            "advance_day / rebuild tasks wait at most this long for an "
            "in-flight run before proceeding."
        ),
    )
    SCHEDULER_RUN_WAIT_POLL_INTERVAL_SECONDS: int = Field(
        default=2,
        gt=0,
        description="Polling cadence inside _wait_for_idle_run.",
    )
    SCHEDULER_WAITER_FLAG_TTL_SECONDS: int = Field(
        default=10 * 60,
        gt=0,
        description=(
            "Auto-expiry on schedule:waiter_pending so a crashed waiter "
            "doesn't permanently suppress retriggers."
        ),
    )

    # --- Logging --------------------------------------------------------------
    LOG_LEVEL: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"

    # ------------------------------------------------------------------ helpers
    @field_validator("CORS_ORIGINS")
    @classmethod
    def _strip_origins(cls, v: str) -> str:
        return v.strip()

    @property
    def cors_origin_list(self) -> list[str]:
        """CORS origins as a clean list (whitespace-trimmed, empties dropped)."""
        return [o.strip() for o in self.CORS_ORIGINS.split(",") if o.strip()]

    @property
    def database_url_str(self) -> str:
        """Database URL coerced to a plain string for SQLAlchemy/Alembic."""
        return str(self.DATABASE_URL)

    @property
    def celery_broker(self) -> str:
        """Effective Celery broker URL (defaults to REDIS_URL when not set)."""
        return str(self.CELERY_BROKER_URL or self.REDIS_URL)

    @property
    def celery_backend(self) -> str:
        """Effective Celery result backend URL (defaults to REDIS_URL)."""
        return str(self.CELERY_RESULT_BACKEND or self.REDIS_URL)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached settings accessor.

    Use this everywhere instead of instantiating `Settings()` directly so that
    (a) we parse env vars only once per process, and (b) tests can override the
    cache via `get_settings.cache_clear()` after monkey-patching env vars.
    """
    return Settings()  # pydantic resolves required fields from env
