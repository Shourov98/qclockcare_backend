"""HTTP middleware: request IDs, timing, structured logging.

Pure ASGI middleware (NOT `BaseHTTPMiddleware`) so it doesn't buffer
streaming responses from inner middleware (e.g. CORSMiddleware's
short-circuit preflight replies, SSE streams from the reports
endpoint). The buffered-response behaviour of `BaseHTTPMiddleware`
strips CORS headers on the way back and is the canonical cause of
"Access-Control-Allow-Origin header is missing" errors even when
CORS middleware is correctly registered. See
https://github.com/encode/starlette/issues/1438 for context.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import structlog

logger = structlog.get_logger(__name__)


class RequestContextMiddleware:
    """Attach a request_id, time the request, and emit a structured log.

    Implemented as raw ASGI middleware rather than `BaseHTTPMiddleware`
    so that:

      - Streaming responses (SSE, `StreamingResponse`) pass through
        unbuffered. BaseHTTPMiddleware loads the body into memory before
        returning, breaking Server-Sent Events.
      - CORS preflight responses from CORSMiddleware keep their
        `Access-Control-Allow-*` headers — BaseHTTPMiddleware's
        response re-encoding drops them. This was the cause of the
        "No Access-Control-Allow-Origin header" errors the SPA saw
        before this change.

    Honours an inbound `X-Request-ID` header if present (for tracing
    across services); otherwise generates a UUIDv4.

    Binds the request_id into structlog's contextvars so all log lines
    emitted during the request automatically include it.

    Returns the same value in the `X-Request-ID` response header.

    Records a `request.complete` log line with the duration, method,
    path, and status.
    """

    def __init__(self, app: Any) -> None:
        self._app = app

    async def __call__(self, scope: dict, receive: Callable, send: Callable) -> None:
        if scope["type"] != "http":
            # Lifespan & websocket: just defer to the inner app.
            await self._app(scope, receive, send)
            return

        # 1. Resolve the request_id before anything else so the entire
        #    request scope (including any inner middleware logs) sees
        #    it via structlog contextvars.
        headers: list[tuple[bytes, bytes]] = scope.get("headers") or []
        request_id: str | None = None
        for name, value in headers:
            if name.lower() == b"x-request-id":
                try:
                    request_id = value.decode("latin-1")
                except UnicodeDecodeError:
                    request_id = None
                break
        if not request_id:
            request_id = str(uuid.uuid4())

        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(request_id=request_id)

        # 2. Stash on `scope` so route handlers can read it.
        scope["state"] = scope.get("state") or {}
        scope["state"]["request_id"] = request_id

        start = time.perf_counter()
        status_holder: dict[str, int] = {"status": 500}

        async def send_wrapper(message: dict) -> None:
            if message["type"] == "http.response.start":
                # Inject X-Request-ID on the way out. `headers` here is
                # a list of (bytes, bytes) tuples.
                status_holder["status"] = message.get("status", 500)
                message.setdefault("headers", [])
                message["headers"] = [
                    *message["headers"],
                    (b"x-request-id", request_id.encode("latin-1")),
                ]
            await send(message)

        try:
            await self._app(scope, receive, send_wrapper)
        except Exception:
            duration_ms = (time.perf_counter() - start) * 1000
            logger.exception(
                "request.error",
                method=scope.get("method"),
                path=scope.get("path"),
                duration_ms=round(duration_ms, 2),
            )
            raise
        else:
            duration_ms = (time.perf_counter() - start) * 1000
            logger.info(
                "request.complete",
                method=scope.get("method"),
                path=scope.get("path"),
                status=status_holder["status"],
                duration_ms=round(duration_ms, 2),
            )


__all__ = ["RequestContextMiddleware"]
