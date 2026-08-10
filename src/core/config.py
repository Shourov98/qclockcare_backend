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

DEPLOYED_CORS_ORIGINS = [
    "https://qlockcare-admin.vercel.app",
    "https://qlockcare-site.vercel.app",
]

LOCAL_CORS_ORIGINS = [
    "http://localhost:3000",
    "http://localhost:3001",
    "http://localhost:5173",
]


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
    # Defaults to the two production Vercel frontends so a fresh deploy
    # doesn't ship with CORS accidentally locked down. Local dev should
    # set `CORS_ORIGINS=http://localhost:3000,http://localhost:3001` in
    # their `.env`; production should set the full allow-list explicitly.
    CORS_ORIGINS: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: [*DEPLOYED_CORS_ORIGINS, *LOCAL_CORS_ORIGINS]
    )
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
    # Retry policy for transactional auth emails (see
    # src/modules/auth/email_service.py:_send_in_background). The
    # background runner retries up to SMTP_RETRY_MAX_ATTEMPTS times
    # with exponential backoff + jitter before giving up and logging
    # at error level. The HTTP response is already flushed at this
    # point, so the user is unaffected by retry duration. Set
    # SMTP_RETRY_MAX_ATTEMPTS=1 to disable retries entirely.
    SMTP_RETRY_MAX_ATTEMPTS: int = Field(default=3, ge=1, le=10)
    SMTP_RETRY_BASE_DELAY_SECONDS: float = Field(default=1.0, ge=0.0, le=60.0)
    SMTP_RETRY_MAX_DELAY_SECONDS: float = Field(default=10.0, ge=0.0, le=600.0)
    # Jitter as a fraction of the computed delay (+/-50% means the
    # actual sleep is uniformly chosen from [0.5*base, 1.5*base]).
    SMTP_RETRY_JITTER: float = Field(default=0.5, ge=0.0, le=1.0)

    # ----- Email (Resend) — ADR-0020 -----
    # Resend is the project's preferred transactional email provider
    # (see ADR-0020). When `RESEND_ENABLED=true` and `RESEND_API_KEY` is
    # set, `EmailProvider` POSTs to `https://api.resend.com/emails`
    # instead of going through aiosmtplib. Falls back to SMTP if Resend
    # is not enabled, then to the dev-log fallback if neither is on.
    RESEND_ENABLED: bool = False
    RESEND_API_KEY: SecretStr | None = None
    # From-address used by the Resend branch. The domain must be
    # verified in the Resend dashboard before mail can be sent from it.
    RESEND_EMAIL: str = "noreply@qlockcare.com"
    # Connect / send timeout for the outbound Resend call. Mirrors
    # `SMTP_TIMEOUT_SECONDS` so the retry loop has a comparable upper
    # bound on per-attempt wall time.
    RESEND_API_TIMEOUT_SECONDS: int = Field(default=10, ge=1, le=120)

    # ----- Frontend (deep links in transactional emails) -----
    # Base URL of the SPA — used by transactional auth emails
    # (OTP verify, password reset) to build a clickable deep link.
    FRONTEND_URL: str = "http://localhost:3000"
    # When True, OTP / reset tokens are logged at INFO with a clear
    # `dev_*` prefix so local dev can test without configuring SMTP.
    # MUST stay False in production.
    LOG_INCLUDE_DEV_OTPS: bool = False

    # ----- Cookies (ADR — HttpOnly auth) -----
    # When True, the `qc_access` / `qc_refresh` cookies are sent with
    # `Secure` (HTTPS-only). MUST stay False in local dev (browsers
    # silently drop Secure cookies on `http://localhost`). Flip on in
    # any environment that's served over TLS.
    COOKIE_SECURE: bool = False
    # SameSite policy for `qc_access` and `qc_csrf`. `lax` is the
    # project default — it allows top-level GET navigations from
    # third-party sites (good UX) while still blocking cross-origin
    # POST/PUT/PATCH/DELETE. Use `strict` if you want full defense
    # against OAuth-style confused-deputy flows.
    COOKIE_SAMESITE: Literal["lax", "strict", "none"] = "lax"
    # Optional cookie domain (e.g. `.qlockcare.com`) so cookies are
    # shared across subdomains. `None` lets the browser default to the
    # exact origin (recommended for single-host deployments).
    COOKIE_DOMAIN: str | None = None

    # ----- SMS (Twilio) — Phase 2 -----
    SMS_ENABLED: bool = False
    TWILIO_ACCOUNT_SID: str | None = None
    TWILIO_AUTH_TOKEN: SecretStr | None = None
    TWILIO_FROM_NUMBER: str | None = None

    # ----- Storage (ADR-0018) -----
    # Default is `supabase` — clients already running on Supabase don't
    # need to set up a separate S3-compatible service. Switch to `s3`
    # for AWS S3 / Floci / MinIO / Cloudflare R2 deployments.
    STORAGE_BACKEND: Literal["s3", "supabase"] = "supabase"
    STORAGE_MAX_FILE_SIZE_MB: int = Field(default=10, ge=1, le=100)
    # `NoDecode` — same as CORS_ORIGINS, we want the raw CSV from env.
    STORAGE_ALLOWED_MIME_TYPES: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["image/png", "image/jpeg", "application/pdf"]
    )
    # Signed-URL TTL — canonical setting shared by every storage backend.
    STORAGE_PRESIGNED_URL_TTL_SECONDS: int = Field(default=900, ge=60, le=86400)

    # ----- S3-Compatible (only used when STORAGE_BACKEND=s3) -----
    S3_ENDPOINT_URL: str | None = None
    S3_REGION: str = "us-east-1"
    S3_ACCESS_KEY_ID: SecretStr = SecretStr("any")
    S3_SECRET_ACCESS_KEY: SecretStr = SecretStr("any")
    S3_FORCE_PATH_STYLE: bool = False
    S3_BUCKET_QUALIFICATIONS: str = "qualifications"
    # Deprecated alias for `STORAGE_PRESIGNED_URL_TTL_SECONDS`. Kept so
    # existing deployments with `S3_PRESIGNED_URL_TTL_SECONDS=...` in
    # their `.env` keep working after the rename.
    S3_PRESIGNED_URL_TTL_SECONDS: int | None = Field(default=None)

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
    # AI narrative generation is expensive (60-90s per call, $0.01-0.10
    # per report). Keep the per-minute budget tight so a runaway script
    # can't burn through the monthly Anthropic allowance.
    RATE_LIMIT_AI_NARRATIVE_PER_MINUTE: int = Field(default=5, ge=1, le=100)

    # ----- Observability -----
    SENTRY_DSN: SecretStr | None = None
    SENTRY_TRACES_SAMPLE_RATE: float = Field(default=0.1, ge=0.0, le=1.0)
    OTEL_ENABLED: bool = False
    OTEL_EXPORTER_OTLP_ENDPOINT: str | None = None

    # ----- AI / LLM (Reports narrative generation) -----
    # Anthropic Claude API key used by the `/reports/{type}/stream` SSE
    # endpoint. When unset (or `FEATURE_REPORTS_AI_NARRATIVE=False`) the
    # endpoint returns 503 with a clear "feature disabled" message —
    # the rest of the system keeps working so CI and unit tests don't
    # need a real key. The orphan `CLAUDE_API_KEY=` line in `.env` is
    # finally picked up here; previously it crashed Settings init
    # because `extra="forbid"` rejected unknown fields.
    CLAUDE_API_KEY: SecretStr | None = None
    # Model id. Sonnet is the default — best cost/quality balance for
    # 4-6k-token narratives. Override with `CLAUDE_MODEL=claude-haiku-...`
    # in `.env` for cheaper bulk runs.
    CLAUDE_MODEL: str = "claude-sonnet-4-5"
    # Per-request timeout. Claude narratives can take 60-90s for the
    # wider report types (Audit Readiness pulls from audit_logs;
    # Group Home synthesizes a placeholder); the upper bound of 600s
    # covers the worst case without leaving a hung request indefinitely.
    CLAUDE_API_TIMEOUT_SECONDS: int = Field(default=120, ge=10, le=600)
    # Max tokens to generate per report. 4k is enough for a tight
    # clinical narrative; bump to 8k for the wider report types.
    CLAUDE_MAX_TOKENS: int = Field(default=4096, ge=256, le=8192)

    # ----- Feature Flags -----
    FEATURE_REGISTRATION_ENABLED: bool = False
    # When True, the `/billing` surface is mounted and Stripe checkout /
    # webhook handlers are live. Leave False until you've set
    # `STRIPE_SECRET_KEY` + `STRIPE_WEBHOOK_SECRET` and run migration 0015.
    FEATURE_BILLING_ENABLED: bool = False
    FEATURE_2FA_ENABLED: bool = False
    # When True, `/reports/{type}/stream` will call Claude. Flip off
    # at runtime to disable AI narrative generation without a deploy
    # (e.g. cost cap reached, provider outage). Mirrors
    # `FEATURE_BILLING_ENABLED`.
    FEATURE_REPORTS_AI_NARRATIVE: bool = True

    # ----- Stripe / Billing (ADR-0021) -----
    # Required when FEATURE_BILLING_ENABLED=True. None in dev keeps the
    # Stripe SDK in test/mock mode so the app starts.
    STRIPE_SECRET_KEY: SecretStr | None = None
    # Used to verify `Stripe-Signature` on inbound webhook deliveries.
    STRIPE_WEBHOOK_SECRET: SecretStr | None = None
    # One Stripe Price ID per agency package — drives Checkout line items.
    STRIPE_PRICE_BASIC: str | None = None
    STRIPE_PRICE_PROFESSIONAL: str | None = None
    STRIPE_PRICE_ENTERPRISE: str | None = None
    # Where Stripe Checkout redirects the customer after success / cancel.
    STRIPE_CHECKOUT_SUCCESS_URL: str = "http://localhost:3000/billing/success"
    STRIPE_CHECKOUT_CANCEL_URL: str = "http://localhost:3000/billing/cancel"
    # Connect timeout for Stripe API calls (seconds). The SDK retries
    # internally up to 2x by default; this caps the outer wait.
    STRIPE_API_TIMEOUT_SECONDS: int = Field(default=15, ge=1, le=120)

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

    @field_validator("CORS_ORIGINS")
    @classmethod
    def _normalize_cors_origins(cls, value: list[str]) -> list[str]:
        """Normalize to browser `Origin` header format.

        Browsers send origins as `scheme://host[:port]` without a trailing
        slash. Starlette's CORS middleware does exact matching, so
        `https://example.com/` does not match `https://example.com`.
        """
        normalized: list[str] = []
        seen: set[str] = set()
        for item in value:
            origin = item.strip().rstrip("/")
            if origin and origin not in seen:
                normalized.append(origin)
                seen.add(origin)
        return normalized

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

    @field_validator("COOKIE_DOMAIN", mode="before")
    @classmethod
    def _empty_domain_to_none(cls, value: object) -> object:
        """Coerce an empty `COOKIE_DOMAIN=` env value to `None` so the
        cookie layer doesn't emit a `Domain=` attribute (which browsers
        interpret as the empty string and refuse to scope the cookie)."""
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("RESEND_API_KEY")
    @classmethod
    def _resend_enabled_needs_key(
        cls, value: SecretStr | None, info
    ) -> SecretStr | None:
        """Refuse to start if Resend is enabled but no API key was set.

        `RESEND_ENABLED=true` with `RESEND_API_KEY=` would crash at send
        time with a 401 from Resend — far less helpful than failing the
        settings load up front. Mirrors the Stripe `STRIPE_SECRET_KEY`
        pattern in this file.
        """
        # `info.data` carries the already-validated sibling fields.
        # `RESEND_ENABLED` is validated before `RESEND_API_KEY` because
        # it appears first in the class body, so it is safe to read.
        enabled = bool(info.data.get("RESEND_ENABLED"))
        if enabled and (value is None or not value.get_secret_value().strip()):
            raise ValueError(
                "RESEND_ENABLED=true but RESEND_API_KEY is empty. "
                "Set RESEND_API_KEY to a valid Resend API key, "
                "or flip RESEND_ENABLED=false to fall back to SMTP / dev-log."
            )
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
    def effective_cors_origins(self) -> list[str]:
        """CORS allow-list used by the HTTP middleware.

        Render deployments may already have an older `CORS_ORIGINS`
        variable set, which overrides the defaults. Keep the known
        production Vercel frontends allowed even when that env var is
        stale, while preserving any explicitly configured origins.
        """
        merged = [*self.CORS_ORIGINS, *DEPLOYED_CORS_ORIGINS]
        normalized: list[str] = []
        seen: set[str] = set()
        for item in merged:
            origin = item.strip().rstrip("/")
            if origin and origin not in seen:
                normalized.append(origin)
                seen.add(origin)
        return normalized

    @property
    def effective_database_url(self) -> str:
        """Pool URL for app runtime, direct URL for Alembic.

        Automatically prepends `+asyncpg` to the scheme if it's missing,
        so deployments that set `DATABASE_URL=postgresql://...` (without
        the driver suffix) still work. Without the explicit driver,
        SQLAlchemy defaults to psycopg2 which isn't installed — causing
        a `ModuleNotFoundError` at engine-creation time on Python 3.14.
        """
        url = (
            self.DATABASE_POOL_URL.get_secret_value()
            if self.DATABASE_POOL_URL is not None
            else self.DATABASE_URL.get_secret_value()
        )
        # Normalize scheme to asyncpg variant for the async runtime.
        # Alembic reads DATABASE_URL separately and swaps its own
        # scheme (asyncpg → psycopg) for sync migrations.
        if url.startswith("postgresql://"):
            url = "postgresql+asyncpg://" + url[len("postgresql://") :]
        # asyncpg refuses the psycopg-only `pgbouncer=true` knob — drop it
        # from the async runtime URL. Supabase's transaction-mode pooler
        # works fine over asyncpg without that flag.
        if "pgbouncer=true" in url:
            from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

            parts = urlsplit(url)
            q = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k != "pgbouncer"]
            url = urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(q), parts.fragment))
        return url

    @property
    def storage_presigned_url_ttl_seconds(self) -> int:
        """Canonical signed-URL TTL, shared by every storage backend.

        Prefers `STORAGE_PRESIGNED_URL_TTL_SECONDS`. Falls back to the
        deprecated `S3_PRESIGNED_URL_TTL_SECONDS` alias so deployments
        that still set the old name keep working after the rename.
        """
        if self.S3_PRESIGNED_URL_TTL_SECONDS is not None:
            return self.S3_PRESIGNED_URL_TTL_SECONDS
        return self.STORAGE_PRESIGNED_URL_TTL_SECONDS

    @property
    def stripe_price_id_for(self) -> dict[str, str | None]:
        """Map AgencySubscriptionPlan.value → Stripe Price ID.

        Returning None for any unmapped plan lets callers raise
        ServiceUnavailableError("Stripe price not configured") at request
        time rather than crashing at startup over a missing env var.
        """
        from src.shared.domain.enums import AgencySubscriptionPlan

        return {
            AgencySubscriptionPlan.BASIC.value: self.STRIPE_PRICE_BASIC,
            AgencySubscriptionPlan.PROFESSIONAL.value: self.STRIPE_PRICE_PROFESSIONAL,
            AgencySubscriptionPlan.ENTERPRISE.value: self.STRIPE_PRICE_ENTERPRISE,
        }

    @property
    def stripe_configured(self) -> bool:
        """True iff billing can talk to Stripe — key + at least one price set.

        The webhook secret is checked separately (the key alone lets us
        create checkout sessions; the secret lets us receive webhooks).

        Both ``None`` and an empty ``SecretStr`` count as "not configured"
        so a deploy that forgets to populate the env var still gets a
        503 rather than a request that crashes mid-flight.
        """
        secret = self.STRIPE_SECRET_KEY
        secret_ok = secret is not None and bool(secret.get_secret_value().strip())
        prices_ok = bool(
            (self.STRIPE_PRICE_BASIC or "")
            or (self.STRIPE_PRICE_PROFESSIONAL or "")
            or (self.STRIPE_PRICE_ENTERPRISE or "")
        )
        return secret_ok and prices_ok

    @property
    def claude_configured(self) -> bool:
        """True iff Claude narrative generation is ready to serve.

        Both the feature flag and the API key must be on — a present
        key alone doesn't mean we should call Claude (e.g. ops may have
        disabled the feature for cost reasons). Mirrors
        `stripe_configured`.
        """
        if not self.FEATURE_REPORTS_AI_NARRATIVE:
            return False
        key = self.CLAUDE_API_KEY
        return key is not None and bool(key.get_secret_value().strip())


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the singleton Settings instance.

    Cached because the app only loads env once. Use `Settings()` directly
    in tests if you need a fresh instance (with monkeypatched env).
    """
    return Settings()  # DATABASE_URL is required; pydantic raises if missing


# Convenience module-level singleton — the canonical way to read settings.
settings = get_settings()
