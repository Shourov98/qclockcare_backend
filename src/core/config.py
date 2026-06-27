"""Application settings — single source of truth for env-driven configuration.

Loaded once at startup via pydantic-settings. All access goes through `settings`,
never `os.getenv`. Validation happens automatically; the app refuses to start
if any required var is missing or invalid.

See `16_ENV_AND_SECRETS.md` for the full variable reference.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Annotated, Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings. All env-driven; immutable after construction."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="forbid",
        validate_default=True,
    )

    # ----- Runtime -----
    APP_ENV: Literal["development", "staging", "production", "test"] = "development"
    APP_NAME: str = "qlockcare-backend"
    APP_VERSION: str = "0.1.0"
    LOG_LEVEL: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    LOG_FORMAT: Literal["json", "console"] = "json"

    # ----- HTTP Server -----
    HOST: str = "0.0.0.0"
    PORT: int = 8000
    # `NoDecode` tells pydantic-settings NOT to JSON-parse this from env —
    # we want the raw comma-separated string so the `_split_csv` validator
    # can split it.
    CORS_ORIGINS: Annotated[list[str], NoDecode] = Field(default_factory=list)
    REQUEST_BODY_SIZE_LIMIT: str = "2mb"

    # ----- Database -----
    DATABASE_URL: SecretStr
    DATABASE_POOL_URL: SecretStr | None = None
    DATABASE_POOL_SIZE: int = Field(default=25, ge=1, le=200)
    DATABASE_MAX_OVERFLOW: int = Field(default=10, ge=0, le=200)
    DATABASE_POOL_TIMEOUT_SECONDS: int = Field(default=30, ge=1, le=300)
    DATABASE_ECHO: bool = False

    # ----- Supabase -----
    SUPABASE_URL: str | None = None
    SUPABASE_ANON_KEY: SecretStr | None = None
    SUPABASE_SERVICE_ROLE_KEY: SecretStr | None = None
    SUPABASE_JWT_SECRET: SecretStr | None = None
    SUPABASE_STORAGE_BUCKET_QUALIFICATIONS: str = "qualifications"

    # ----- JWT -----
    JWT_ALGORITHM: Literal["HS256", "RS256"] = "HS256"
    JWT_PRIVATE_KEY: SecretStr | None = None  # RS256 only
    JWT_PUBLIC_KEY: SecretStr | None = None  # RS256 only
    JWT_ACCESS_TOKEN_TTL_MINUTES: int = Field(default=15, ge=1, le=60)
    JWT_REFRESH_TOKEN_TTL_DAYS: int = Field(default=7, ge=1, le=30)
    JWT_ISSUER: str = "qlockcare"
    JWT_AUDIENCE: str = "qlockcare-api"

    # ----- Password / Auth -----
    PASSWORD_HASH_ALGORITHM: Literal["argon2", "bcrypt"] = "argon2"
    PASSWORD_MIN_LENGTH: int = Field(default=12, ge=8, le=128)
    PASSWORD_REQUIRE_UPPERCASE: bool = True
    PASSWORD_REQUIRE_LOWERCASE: bool = True
    PASSWORD_REQUIRE_DIGIT: bool = True
    PASSWORD_REQUIRE_SYMBOL: bool = True
    ACCOUNT_LOCKOUT_THRESHOLD: int = Field(default=5, ge=1, le=20)
    ACCOUNT_LOCKOUT_DURATION_MINUTES: int = Field(default=15, ge=1, le=1440)

    # ----- Email Verification (OTP) — ADR-0016 -----
    OTP_LENGTH: int = Field(default=4, ge=4, le=8)
    OTP_EXPIRY_MINUTES: int = Field(default=10, ge=1, le=60)
    OTP_MAX_ATTEMPTS: int = Field(default=5, ge=1, le=10)
    OTP_RESEND_COOLDOWN_SECONDS: int = Field(default=60, ge=10, le=600)
    OTP_RESEND_MAX_PER_HOUR: int = Field(default=5, ge=1, le=20)
    OTP_RESEND_MAX_PER_DAY: int = Field(default=20, ge=1, le=100)
    INVITATION_TOKEN_EXPIRY_DAYS: int = Field(default=7, ge=1, le=30)

    # ----- Email (SMTP) -----
    SMTP_ENABLED: bool = False
    SMTP_HOST: str = "localhost"
    SMTP_PORT: int = 1025
    SMTP_USERNAME: str = ""
    SMTP_PASSWORD: SecretStr | None = None
    SMTP_FROM_EMAIL: str = "noreply@qlockcare.local"
    SMTP_FROM_NAME: str = "QlockCare"
    SMTP_USE_TLS: bool = False
    # Connect/send timeout for aiosmtplib. Background-task dispatch
    # also relies on this to avoid unbounded hangs in worker threads.
    SMTP_TIMEOUT_SECONDS: int = Field(default=10, ge=1, le=120)

    # ----- Frontend (deep links in transactional emails) -----
    # Base URL of the SPA — used by transactional auth emails
    # (OTP verify, password reset) to build a clickable deep link.
    FRONTEND_URL: str = "http://localhost:3000"
    # When True, OTP / reset tokens are logged at INFO with a clear
    # `dev_*` prefix so local dev can test without configuring SMTP.
    # MUST stay False in production.
    LOG_INCLUDE_DEV_OTPS: bool = False

    # ----- SMS (Twilio) — Phase 2 -----
    SMS_ENABLED: bool = False
    TWILIO_ACCOUNT_SID: str | None = None
    TWILIO_AUTH_TOKEN: SecretStr | None = None
    TWILIO_FROM_NUMBER: str | None = None

    # ----- Storage (ADR-0018) -----
    STORAGE_BACKEND: Literal["s3", "supabase"] = "s3"
    STORAGE_MAX_FILE_SIZE_MB: int = Field(default=10, ge=1, le=100)
    # `NoDecode` — same as CORS_ORIGINS, we want the raw CSV from env.
    STORAGE_ALLOWED_MIME_TYPES: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["image/png", "image/jpeg", "application/pdf"]
    )

    # ----- S3-Compatible -----
    S3_ENDPOINT_URL: str | None = None
    S3_REGION: str = "us-east-1"
    S3_ACCESS_KEY_ID: SecretStr = SecretStr("any")
    S3_SECRET_ACCESS_KEY: SecretStr = SecretStr("any")
    S3_FORCE_PATH_STYLE: bool = False
    S3_BUCKET_QUALIFICATIONS: str = "qualifications"
    S3_PRESIGNED_URL_TTL_SECONDS: int = Field(default=900, ge=60, le=86400)

    # ----- Notifications -----
    NOTIFICATION_RETRY_MAX_ATTEMPTS: int = Field(default=3, ge=1, le=10)
    NOTIFICATION_RETRY_BACKOFF_SECONDS: int = Field(default=60, ge=1, le=3600)
    NOTIFICATION_BATCH_SIZE: int = Field(default=50, ge=1, le=500)
    # How long the unread badge can be cached. Currently a no-op —
    # the badge endpoint hits the DB on every request. When Redis is
    # wired up, the badge endpoint should cache `unread_count` under
    # the user_id with this TTL.
    NOTIFICATION_BADGE_CACHE_TTL_SECONDS: int = Field(default=30, ge=0, le=3600)

    # ----- Rate Limiting -----
    RATE_LIMIT_ENABLED: bool = True
    RATE_LIMIT_PER_MINUTE: int = Field(default=60, ge=1, le=10000)
    RATE_LIMIT_LOGIN_PER_MINUTE: int = Field(default=10, ge=1, le=100)
    RATE_LIMIT_LOGIN_PER_HOUR: int = Field(default=50, ge=1, le=1000)
    RATE_LIMIT_VERIFY_EMAIL_PER_MINUTE: int = Field(default=10, ge=1, le=100)
    RATE_LIMIT_VERIFY_EMAIL_PER_HOUR: int = Field(default=50, ge=1, le=1000)
    RATE_LIMIT_RESEND_PER_MINUTE: int = Field(default=5, ge=1, le=100)
    RATE_LIMIT_RESEND_PER_HOUR: int = Field(default=20, ge=1, le=1000)
    RATE_LIMIT_ACCEPT_INVITATION_PER_MINUTE: int = Field(default=10, ge=1, le=100)
    RATE_LIMIT_REFRESH_PER_MINUTE: int = Field(default=30, ge=1, le=1000)

    # ----- Observability -----
    SENTRY_DSN: SecretStr | None = None
    SENTRY_TRACES_SAMPLE_RATE: float = Field(default=0.1, ge=0.0, le=1.0)
    OTEL_ENABLED: bool = False
    OTEL_EXPORTER_OTLP_ENDPOINT: str | None = None

    # ----- Feature Flags -----
    FEATURE_REGISTRATION_ENABLED: bool = False
    FEATURE_BILLING_ENABLED: bool = False
    FEATURE_2FA_ENABLED: bool = False

    # ----- Seed / Bootstrap -----
    SEED_SUPER_ADMIN_EMAIL: str | None = None
    SEED_SUPER_ADMIN_PASSWORD: SecretStr | None = None
    SEED_DEMO_AGENCY_NAME: str = "Demo Home Care"
    SEED_DEMO_AGENCY_TIMEZONE: str = "America/Chicago"

    # ------------------------------------------------------------------
    # Validators
    # ------------------------------------------------------------------
    @field_validator("CORS_ORIGINS", "STORAGE_ALLOWED_MIME_TYPES", mode="before")
    @classmethod
    def _split_csv(cls, value: object) -> object:
        """Allow comma-separated strings from env files."""
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @field_validator("STORAGE_ALLOWED_MIME_TYPES")
    @classmethod
    def _at_least_one_mime(cls, value: list[str]) -> list[str]:
        if not value:
            raise ValueError("STORAGE_ALLOWED_MIME_TYPES must contain at least one MIME type")
        return value

    @field_validator("SUPABASE_JWT_SECRET")
    @classmethod
    def _jwt_secret_long_enough(cls, value: SecretStr | None) -> SecretStr | None:
        if value is not None and len(value.get_secret_value()) < 32:
            raise ValueError("SUPABASE_JWT_SECRET must be at least 32 characters")
        return value

    @field_validator("JWT_ALGORITHM")
    @classmethod
    def _rs256_needs_keys(cls, value: str, info) -> str:  # type: ignore[no-untyped-def]
        # The RS256 / keys cross-field validation happens after construction
        # (Pydantic v2 makes cross-field checks awkward in a single validator).
        return value

    # ------------------------------------------------------------------
    # Derived properties
    # ------------------------------------------------------------------
    @property
    def is_production(self) -> bool:
        return self.APP_ENV == "production"

    @property
    def is_development(self) -> bool:
        return self.APP_ENV == "development"

    @property
    def is_test(self) -> bool:
        return self.APP_ENV == "test"

    @property
    def effective_database_url(self) -> str:
        """Pool URL for app runtime, direct URL for Alembic."""
        return (
            self.DATABASE_POOL_URL.get_secret_value()
            if self.DATABASE_POOL_URL is not None
            else self.DATABASE_URL.get_secret_value()
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the singleton Settings instance.

    Cached because the app only loads env once. Use `Settings()` directly
    in tests if you need a fresh instance (with monkeypatched env).
    """
    return Settings()  # DATABASE_URL is required; pydantic raises if missing


# Convenience module-level singleton — the canonical way to read settings.
settings = get_settings()
