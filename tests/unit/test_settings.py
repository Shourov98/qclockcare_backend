"""Settings unit tests — verify env parsing + validation.

Uses `settings_override` to flip individual env vars and rebuild the
singleton via `config.get_settings.cache_clear()`.
"""

from __future__ import annotations

import pytest


def test_settings_load_with_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    """Settings load from the test env (set by conftest)."""
    from src.core import config

    config.get_settings.cache_clear()
    settings = config.get_settings()

    assert settings.is_test
    assert settings.APP_NAME == "qlockcare-backend"


def test_effective_database_url_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    """If DATABASE_POOL_URL is unset, fall back to DATABASE_URL."""
    from src.core import config

    monkeypatch.delenv("DATABASE_POOL_URL", raising=False)
    config.get_settings.cache_clear()
    settings = config.get_settings()

    assert settings.effective_database_url == settings.DATABASE_URL.get_secret_value()


def test_cors_origins_csv_parsing(monkeypatch: pytest.MonkeyPatch) -> None:
    """CORS_ORIGINS accepts a comma-separated string from env."""
    from src.core import config

    monkeypatch.setenv("CORS_ORIGINS", "http://localhost:3000,http://localhost:5173")
    config.get_settings.cache_clear()
    settings = config.get_settings()

    assert settings.CORS_ORIGINS == [
        "http://localhost:3000",
        "http://localhost:5173",
    ]


def test_storage_backend_defaults_to_s3(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default storage backend is S3-compatible (Floci/AWS)."""
    from src.core import config

    config.get_settings.cache_clear()
    settings = config.get_settings()

    assert settings.STORAGE_BACKEND == "s3"
