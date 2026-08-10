"""Rate-limit singleton — shared across the app.

The authoritative `Limiter` instance is built in `main._build_limiter()`
and stored on `app.state.limiter` (slowapi's required location). This
module exposes a second reference to the same instance so route
handlers can use `@limiter.limit(...)` without re-importing slowapi
plumbing.

The `Limiter` is constructed lazily on first access — at import time
we don't yet know if `settings.RATE_LIMIT_ENABLED` is on, but more
importantly we don't want `core.rate_limit` to be imported during
`Settings` validation (which would create a circular dependency:
`rate_limit` → `config` → `lru_cache` → `Settings` → …).

Usage in a router:

    from src.core.rate_limit import limiter

    @router.post("/foo")
    @limiter.limit("5/minute")
    async def post_foo(request: Request, ...): ...

The `request: Request` parameter is required by slowapi's
`key_func=get_remote_address` — it reads `request.client.host`.
"""

from __future__ import annotations

from typing import Any

# Lazy singleton — built on first `limiter` access so the
# `Settings()` lru_cache doesn't see an import loop.
_limiter: Any = None


def _get_limiter() -> Any:
    """Return the lazily-built limiter instance.

    `Limiter` is constructed with the same args `main._build_limiter()` uses
    so the module-level singleton and the `app.state.limiter` instance are
    functionally identical (same storage backend, same key func).
    """
    global _limiter
    if _limiter is None:
        from slowapi import Limiter
        from slowapi.util import get_remote_address

        from src.core.config import settings

        _limiter = Limiter(
            key_func=get_remote_address,
            enabled=settings.RATE_LIMIT_ENABLED,
            headers_enabled=True,
        )
    return _limiter


class _LimiterProxy:
    """Proxy that lazily forwards attribute access to the singleton.

    Lets module-level decorators like `@limiter.limit("5/minute")`
    work without an explicit `limiter = ...` at import time. The
    decorator machinery in slowapi is sticky on the wrapped function
    rather than on the proxy itself, so this works.
    """

    def __getattr__(self, name: str) -> Any:
        return getattr(_get_limiter(), name)


limiter: Any = _LimiterProxy()


__all__ = ["limiter"]
