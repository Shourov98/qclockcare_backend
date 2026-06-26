"""Shared pytest fixtures.

The biggest fixture here is `client` — a `TestClient` against the real
FastAPI app. The lifespan will attempt a DB ping; in the absence of a real
Postgres it logs an error but does not raise. Tests that need a real DB
should use the `integration` marker and a docker-compose dev DB.

We load `.env` first so the local Supabase URL (port 54322) wins over the
fallback below (port 5432). `setdefault` is used everywhere so a developer
with their own `.env` overrides don't get clobbered.
"""

from __future__ import annotations

# ---- env-var defaults MUST come first (before any src.* import) ----
import os
from pathlib import Path

# Load .env into os.environ FIRST so DATABASE_URL wins.
_ENV_PATH = Path(__file__).resolve().parents[1] / ".env"
if _ENV_PATH.exists():
    for _line in _ENV_PATH.read_text().splitlines():
        # Strip inline comments and surrounding whitespace
        _line = _line.split("#", 1)[0].strip()
        if not _line or "=" not in _line:
            continue
        _k, _, _v = _line.partition("=")
        # Strip surrounding whitespace and quotes from the value
        _v = _v.strip().strip('"').strip("'")
        os.environ.setdefault(_k.strip(), _v)

# Fallbacks for CI / unit tests without a real DB.
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
