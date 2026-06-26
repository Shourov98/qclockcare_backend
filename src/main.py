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
from src.modules.identity.router import router as auth_router
from src.modules.patients.router import router as patients_router
from src.modules.staff.router import router as staff_router

# Import model modules so all mappers register on Base.metadata before any
# query runs. SQLAlchemy resolves `relationship("Agency")` lazily, but the
# resolution has to happen before the first mapper is configured against
# the registry.
from src.modules.agencies.models import Agency as _Agency  # noqa: F401
from src.modules.identity.models import (  # noqa: F401
    AuthAuditEvent,
    EmailVerificationOtp,
    RefreshToken,
    SingleUseToken,
    User,
    UserRoleAssignment,
)
from src.modules.staff.models import (  # noqa: F401
    StaffAvailability,
    StaffProfile,
    StaffQualification,
)
from src.modules.patients.models import (  # noqa: F401
    GuardianProfile,
    PatientGuardianRelationship,
    PatientProfile,
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
        debug=settings.is_development,
        lifespan=lifespan,
        docs_url="/docs" if not settings.is_production else None,
        redoc_url="/redoc" if not settings.is_production else None,
        openapi_url="/openapi.json" if not settings.is_production else None,
    )

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
