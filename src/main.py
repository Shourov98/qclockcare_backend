"""FastAPI application factory.

`create_app()` is the single entry point — uvicorn imports the module and
calls `app = create_app()`. All wiring (logging, middleware, handlers,
routers, lifespan) goes through here. Keep this file thin and composable;
features live under `src/modules/*` and get included as routers.

See `09_BACKEND_STRUCTURE_SOLID_OOP.md` for the layering rationale.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from slowapi.util import get_remote_address
from starlette.middleware.trustedhost import TrustedHostMiddleware

from src.core.config import settings
from src.core.database import dispose_engine
from src.core.exceptions import register_exception_handlers
from src.core.health import router as health_router
from src.core.logging import configure_logging
from src.core.middleware import RequestContextMiddleware

# Import model modules so all mappers register on Base.metadata before any
# query runs. SQLAlchemy resolves `relationship("Agency")` lazily, but the
# resolution has to happen before the first mapper is configured against
# the registry.
from src.modules.agencies.models import Agency as _Agency  # noqa: F401
from src.modules.agencies.router import router as agencies_router
from src.modules.appointments.models import (  # noqa: F401
    Appointment,
    AppointmentServiceItem,
)
from src.modules.appointments.router import router as appointments_router
from src.modules.audit_logs.models import AuditLog  # noqa: F401
from src.modules.audit_logs.router import router as audit_logs_router
from src.modules.identity.models import (  # noqa: F401
    AuthAuditEvent,
    EmailVerificationOtp,
    RefreshToken,
    SingleUseToken,
    User,
    UserRoleAssignment,
)
from src.modules.identity.router import router as auth_router
from src.modules.locations.models import Location  # noqa: F401
from src.modules.locations.router import router as locations_router
from src.modules.notifications.models import Notification  # noqa: F401
from src.modules.notifications.router import router as notifications_router
from src.modules.patients.models import (  # noqa: F401
    GuardianProfile,
    PatientGuardianRelationship,
    PatientProfile,
)
from src.modules.patients.router import router as patients_router
from src.modules.portal.router import router as portal_router
from src.modules.staff.models import (  # noqa: F401
    StaffAvailability,
    StaffProfile,
    StaffQualification,
)
from src.modules.staff.router import router as staff_router
from src.modules.visits.models import (  # noqa: F401
    ServiceVerification,
    Visit,
    VisitIssue,
    VisitNote,
    VisitServiceItem,
)
from src.modules.visits.router import router as visits_router
from src.shared.schemas.docs import (
    OPENAPI_SECURITY,
    OPENAPI_SECURITY_SCHEMES,
    tags_metadata,
)

logger = structlog.get_logger(__name__)


# --------------------------------------------------------------------------
# Rate limiter (slowapi) — built once per app
# --------------------------------------------------------------------------
def _build_limiter() -> Limiter:
    """Build the shared slowapi limiter.

    The limiter is stored on `app.state.limiter` (required by slowapi) and
    the `SlowAPIMiddleware` reads it from there.
    """
    return Limiter(
        key_func=get_remote_address,
        default_limits=(
            [f"{settings.RATE_LIMIT_PER_MINUTE}/minute"] if settings.RATE_LIMIT_ENABLED else []
        ),
        headers_enabled=True,
    )


# --------------------------------------------------------------------------
# Lifespan — startup + shutdown hooks
# --------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Manage resources tied to the app process.

    Startup:
        - Configure logging
        - Probe the DB so misconfiguration fails fast (warn-only, doesn't raise)
        - Log effective config summary

    Shutdown:
        - Dispose the engine and close pooled connections
    """
    configure_logging()

    logger.info(
        "startup.begin",
        app=settings.APP_NAME,
        version=settings.APP_VERSION,
        env=settings.APP_ENV,
        storage_backend=settings.STORAGE_BACKEND,
    )

    # Probe DB connectivity — don't crash startup; let /ready report unready.
    try:
        from sqlalchemy import text

        from src.core.database import engine

        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        logger.info("startup.database_ok")
    except Exception as exc:
        logger.error(
            "startup.database_unreachable",
            error=type(exc).__name__,
            message=str(exc),
        )

    logger.info("startup.complete")

    try:
        yield
    finally:
        logger.info("shutdown.begin")
        await dispose_engine()
        logger.info("shutdown.complete")


# --------------------------------------------------------------------------
# OpenAPI metadata — long-form description shown on /docs and /redoc
# --------------------------------------------------------------------------
_API_DESCRIPTION = """
QlockCare backend API.

## Authentication

All endpoints except the public auth flows (`POST /auth/login`,
`POST /auth/refresh`, `POST /auth/forgot-password`,
`POST /auth/reset-password`, `POST /auth/accept-invitation`,
`POST /auth/verify-email`, `POST /auth/resend-otp`) require a Bearer
JWT in the `Authorization` header. Use the **Authorize** button at
the top of `/docs` to paste your access token once for the whole
session.

The token has a short lifetime (default 15 minutes); refresh it via
`POST /auth/refresh` with your current refresh token.

## Errors

Every non-2xx response uses the standard error envelope:

```json
{
  "error": {
    "code": "VALIDATION_ERROR",
    "message": "Request body failed validation.",
    "request_id": "5f3a7b1c-1d0a-4a23-9c8e-1b2c3d4e5f6a",
    "timestamp": "2026-06-28T10:23:01Z",
    "details": [{"field": "email", "message": "...", "type": "..."}]
  }
}
```

`code` is stable across releases — branch on it, not on
`message`. `request_id` echoes the `X-Request-ID` response header;
include it in support tickets. `details` is populated for `422`
validation errors (one entry per failing field) and for typed
domain errors like `INSUFFICIENT_PERMISSIONS`.

## Pagination

List endpoints use offset pagination via `?page=` and
`?page_size=` (default 20, max 100). The cursor-based variants
(notifications, audit logs) accept `?cursor=` and `?limit=`.

## Rate limiting

Per-IP rate limit (default 60 req/min) is enforced by slowapi.
Exceeding the limit returns `429 RATE_LIMIT_EXCEEDED` with the
standard error envelope.
"""


def _custom_openapi() -> dict:
    """Build the OpenAPI schema with the project's auth scheme attached.

    FastAPI's default `openapi()` doesn't include the
    `components.securitySchemes` block or a top-level `security`
    entry — both of which Swagger UI needs to render the
    "Authorize" button. We:

      1. Call FastAPI's own `get_openapi(...)` (handles all the
         route -> spec translation correctly).
      2. Inject `OPENAPI_SECURITY_SCHEMES` into `components`.
      3. Set a global `security` list so Swagger UI defaults to
         "this endpoint requires Bearer auth" — the few public
         auth routes are unaffected because their handlers run
         regardless of the `security` block.
      4. Cache the result on `app.openapi_schema` so subsequent
         calls (per request) are O(1).

    Without this, Swagger UI shows the bearer field greyed-out and
    "Try it out" silently strips the `Authorization` header.
    """
    from fastapi.openapi.utils import get_openapi

    if app.openapi_schema:
        return app.openapi_schema
    schema = get_openapi(
        title=app.title,
        version=app.version,
        openapi_version=app.openapi_version,
        description=app.description,
        routes=app.routes,
    )
    # FastAPI's `get_openapi()` does NOT forward `contact` /
    # `license_info` from the constructor into the schema, and it
    # does NOT surface `openapi_tags` as a top-level `tags` array.
    # Inject both manually so Swagger UI's sidebar shows the tag
    # descriptions and the info block carries the contact / license.
    schema["info"]["contact"] = {
        "name": "QlockCare Engineering",
        "email": "eng@qlockcare.com",
    }
    schema["info"]["licenseInfo"] = {"name": "Proprietary"}
    schema["tags"] = list(tags_metadata)
    schema.setdefault("components", {})
    schema["components"]["securitySchemes"] = OPENAPI_SECURITY_SCHEMES
    schema["security"] = OPENAPI_SECURITY
    app.openapi_schema = schema
    return schema


# --------------------------------------------------------------------------
# Application factory
# --------------------------------------------------------------------------
def create_app() -> FastAPI:
    """Build and configure the FastAPI app.

    Order of operations matters:
        1. Build the FastAPI instance (with lifespan)
        2. Register exception handlers
        3. Add CORS / trusted-host / request-context middleware
        4. Add slowapi rate-limit middleware + state
        5. Include routers (health first — it's always on)
    """
    app = FastAPI(
        title=settings.APP_NAME,
        version=settings.APP_VERSION,
        description=_API_DESCRIPTION,
        contact={
            "name": "QlockCare Engineering",
            "email": "eng@qlockcare.com",
        },
        license_info={"name": "Proprietary"},
        debug=settings.is_development,
        lifespan=lifespan,
        openapi_tags=tags_metadata,
        # Swagger UI tweaks — keep the bearer token across reloads,
        # show how long each request took (helps during dev), and
        # enable the top-bar search filter for routes.
        swagger_ui_parameters={
            "persistAuthorization": True,
            "displayRequestDuration": True,
            "filter": True,
        },
        docs_url="/docs" if not settings.is_production else None,
        redoc_url="/redoc" if not settings.is_production else None,
        openapi_url="/openapi.json" if not settings.is_production else None,
    )

    # Install the custom OpenAPI generator so `/openapi.json` carries
    # the `components.securitySchemes` block (Swagger UI's "Authorize"
    # button needs this to work). Must be set BEFORE any client calls
    # `app.openapi()` — i.e. before the first request or test.
    app.openapi = _custom_openapi

    # ---- Exception handlers ----
    register_exception_handlers(app)

    # ---- CORS ----
    if settings.CORS_ORIGINS:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.CORS_ORIGINS,
            allow_credentials=True,
            allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
            allow_headers=["Authorization", "Content-Type", "X-Request-ID"],
            expose_headers=[
                "X-Request-ID",
                "X-RateLimit-Limit",
                "X-RateLimit-Remaining",
            ],
        )
    else:
        logger.warning("cors.disabled", reason="CORS_ORIGINS is empty")

    # ---- Trusted hosts (production only) ----
    if settings.is_production:
        app.add_middleware(
            TrustedHostMiddleware,
            allowed_hosts=["*"],  # tighten via reverse proxy / ingress in real prod
        )

    # ---- Rate limiting (built before middleware so the limiter is on app.state) ----
    app.state.limiter = _build_limiter()
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
    app.add_middleware(SlowAPIMiddleware)

    # ---- Request context (added last so it runs FIRST in the middleware chain) ----
    # NOTE: Starlette executes middleware in reverse order of registration.
    # Registering RequestContextMiddleware last ensures it wraps everything else
    # and produces a request_id before any other middleware logs.
    app.add_middleware(RequestContextMiddleware)

    # ---- Routers ----
    # Health is always exposed (k8s probes never auth).
    app.include_router(health_router)

    # Auth — register with no extra prefix (router already uses /auth).
    app.include_router(auth_router)

    # Staff — agency-scoped staff profiles, qualifications, availability.
    app.include_router(staff_router)

    # Patients + guardians + relationships.
    app.include_router(patients_router)

    # Appointments + service items.
    app.include_router(appointments_router)

    # Visits + service items + verification + issues.
    app.include_router(visits_router)

    # Patient/Guardian portal — verify/dispute/report-issue surface.
    app.include_router(portal_router)

    # Notifications — recipient-facing list/read endpoints.
    app.include_router(notifications_router)

    # Locations — service-delivery addresses (used by appointments/visits).
    app.include_router(locations_router)

    # Audit logs — admin-facing list/get endpoints.
    app.include_router(audit_logs_router)

    # Agencies — SUPER_ADMIN-only management of agency tenants.
    app.include_router(agencies_router)

    # NOTE: feature routers get registered here as modules land, e.g.
    #   app.include_router(staff_router, prefix="/staff", tags=["staff"])

    return app


# --------------------------------------------------------------------------
# Module-level app instance — what uvicorn imports
# --------------------------------------------------------------------------
app = create_app()


if __name__ == "__main__":  # pragma: no cover
    import uvicorn

    uvicorn.run(
        "src.main:app",
        host=settings.HOST,
        port=settings.PORT,
        reload=settings.is_development,
        log_level=settings.LOG_LEVEL.lower(),
    )


__all__ = ["app", "create_app", "lifespan"]
