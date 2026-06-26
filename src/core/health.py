"""Health and readiness endpoints.

`/health`  — liveness probe. Always 200 unless the process is broken.
`/ready`   — readiness probe. Pings the DB; returns 503 if unreachable.
"""

from __future__ import annotations

from fastapi import APIRouter, Response, status
from sqlalchemy import text

from src.core.config import settings
from src.core.database import engine

router = APIRouter(tags=["health"])


@router.get("/health", summary="Liveness probe")
async def health() -> dict[str, str]:
    """Always returns 200 if the process is up.

    Use this for Kubernetes liveness probes / load balancer health checks.
    Do NOT do expensive work here (no DB ping, no external calls).
    """
    return {
        "status": "ok",
        "app": settings.APP_NAME,
        "version": settings.APP_VERSION,
        "env": settings.APP_ENV,
    }


@router.get("/ready", summary="Readiness probe")
async def ready(response: Response) -> dict[str, object]:
    """Returns 200 if the app can serve traffic; 503 otherwise.

    Currently checks the DB connection. Add more checks here (SMTP, S3)
    if you want stricter readiness.
    """
    checks: dict[str, str] = {}

    # Database check
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        checks["database"] = "ok"
    except Exception as exc:
        checks["database"] = f"error: {type(exc).__name__}"

    overall_ok = all(value == "ok" for value in checks.values())
    response.status_code = status.HTTP_200_OK if overall_ok else status.HTTP_503_SERVICE_UNAVAILABLE
    return {
        "status": "ok" if overall_ok else "degraded",
        "checks": checks,
    }


__all__ = ["router"]
