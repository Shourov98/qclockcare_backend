"""Shared pytest fixtures.

The biggest fixture here is `client` — a `TestClient` against the real
FastAPI app. The lifespan will attempt a DB ping; in the absence of a real
Postgres it logs an error but does not raise. Tests that need a real DB
should use the `integration` marker and a docker-compose dev DB.

IMPORTANT: env-var defaults are set at module import time (top of file),
BEFORE any other imports, so that `src.core.config.settings` — which is
a module singleton built at import time — sees a valid `DATABASE_URL`.
"""

from __future__ import annotations

# ---- env-var defaults MUST come first (before any src.* import) ----
import os

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault(
    "DATABASE_URL",
    "postgresql+asyncpg://test:test@localhost:5432/test",
)

from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(scope="function")
def client() -> Iterator[TestClient]:
    """A TestClient bound to the FastAPI app.

    The lifespan attempts a DB ping; if Postgres is unreachable it logs
    an error but doesn't crash. `/health` works regardless. `/ready`
    will return 503 in this environment — that's expected.
    """
    from src.main import app

    with TestClient(app) as tc:
        yield tc


@pytest.fixture
def settings_override(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Helper to override an env var and force settings reload."""
    from src.core import config

    def _set(key: str, value: str) -> None:
        monkeypatch.setenv(key, value)
        config.get_settings.cache_clear()

    return _set
