"""Async SQLAlchemy engine + session factory.

Every request gets a fresh session via the `get_session` dependency.
Sessions auto-commit on success and auto-rollback on exception.

Set-per-request session vars (e.g. `app.current_user_id`, `app.current_agency_id`)
are configured via the `set_session_context` helper used in middleware.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from src.core.config import settings
from src.core.logging import get_logger

logger = get_logger(__name__)


# --------------------------------------------------------------------------
# Engine + session factory (module-level singletons)
# --------------------------------------------------------------------------
def _build_engine() -> AsyncEngine:
    """Create the async engine from the pool URL (app runtime)."""
    return create_async_engine(
        settings.effective_database_url,
        echo=settings.DATABASE_ECHO,
        pool_size=settings.DATABASE_POOL_SIZE,
        max_overflow=settings.DATABASE_MAX_OVERFLOW,
        pool_timeout=settings.DATABASE_POOL_TIMEOUT_SECONDS,
        pool_pre_ping=True,  # validate connections before use
        future=True,
    )


engine: AsyncEngine = _build_engine()

AsyncSessionLocal: async_sessionmaker[AsyncSession] = async_sessionmaker(
    bind=engine,
    expire_on_commit=False,  # attributes remain accessible after commit
    autoflush=False,
)


# --------------------------------------------------------------------------
# Per-request session
# --------------------------------------------------------------------------
async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency yielding an AsyncSession.

    Usage:
        @router.get(...)
        async def handler(session: AsyncSession = Depends(get_session)):
            ...

    The session is committed if no exception, rolled back if any. The
    `session_scope` context manager wraps the same logic for non-FastAPI code.
    """
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except SQLAlchemyError:
            await session.rollback()
            raise
        except Exception:
            await session.rollback()
            raise


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """Async context manager equivalent of `get_session` for scripts / background jobs."""
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


# --------------------------------------------------------------------------
# RLS session context
# --------------------------------------------------------------------------
async def set_session_context(
    session: AsyncSession,
    *,
    user_id: str | None = None,
    agency_id: str | None = None,
    user_role: str | None = None,
) -> None:
    """Set Postgres session vars used by RLS policies.

    Call this in middleware before any tenant-scoped query runs. The values
    are read by `current_setting('app.current_user_id')` etc. inside policies
    (see `14_RLS_AND_MULTITENANCY.md`).
    """
    params: dict[str, Any] = {}
    if user_id is not None:
        params["app.current_user_id"] = user_id
    if agency_id is not None:
        params["app.current_agency_id"] = agency_id
    if user_role is not None:
        params["app.current_user_role"] = user_role
    if params:
        # set_config(setting_name, value, is_local) — is_local=true means
        # the change reverts at end of transaction, which is exactly what
        # we want per-request.
        from sqlalchemy import text

        for key, value in params.items():
            await session.execute(
                text("SELECT set_config(:k, :v, true)"),
                {"k": key, "v": str(value)},
            )


async def dispose_engine() -> None:
    """Close all pooled connections. Call on app shutdown."""
    await engine.dispose()


__all__ = [
    "AsyncSessionLocal",
    "dispose_engine",
    "engine",
    "get_session",
    "session_scope",
    "set_session_context",
]
