"""Reports router.

Endpoints:

  * `POST /reports/{report_type}/stream` — SSE stream of Claude tokens.
    Returns `text/event-stream`; each frame is `data: <json>\n\n` with
    a `StreamEvent` payload (kind='run_meta' | 'delta' | 'final' | 'error').
    Auth-required (AGENCY_ADMIN only); rate-limited via
    `settings.RATE_LIMIT_AI_NARRATIVE_PER_MINUTE`.

  * `GET  /reports/runs` — list the agency's recent runs (sidebar).
  * `GET  /reports/runs/{run_id}` — read one run (incl. full narrative).

  * `GET  /reports/runs/{run_id}/export?format=pdf|csv|xlsx` —
    `StreamingResponse` with `Content-Disposition: attachment`.

The router is mounted in `src/main.py` alongside the others, gated on
the `claude_configured` property. When the key is missing or the
feature flag is off, `/stream` returns 503 with a clear message; the
read endpoints stay available so the UI can still show history.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.config import settings
from src.core.exceptions import NotFoundError, ServiceUnavailableError
from src.core.logging import get_logger
from src.core.rate_limit import limiter
from src.modules.identity.dependencies import (
    CurrentAuth,
    get_session_with_auth,
)
from src.modules.reports import service as reports_service
from src.modules.reports.schemas import (
    ReportRunCreate,
    ReportRunRead,
    ReportRunSummary,
    ReportType,
    StreamEvent,
)
from src.modules.reports.service import ReportService
from src.shared.schemas.docs import standard_responses

log = get_logger(__name__)


# --------------------------------------------------------------------------
# Router
# --------------------------------------------------------------------------
router = APIRouter(prefix="/reports", tags=["reports"])


def _reports_ai_enabled_or_503() -> None:
    """Per-request gate mirroring `billing/router.py:_billing_enabled_or_503`."""
    if not settings.claude_configured:
        raise ServiceUnavailableError(
            "AI narrative generation is disabled. "
            "Set FEATURE_REPORTS_AI_NARRATIVE=true and CLAUDE_API_KEY=<key>.",
        )


def _parse_report_type(raw: str) -> ReportType:
    """Parse a URL-path report type into the enum.

    Raises NotFoundError (404) on miss — FastAPI's default 422 for an
    invalid path param is noisier and doesn't match the project's
    standard error envelope.
    """
    try:
        return ReportType(raw)
    except ValueError:
        raise NotFoundError(
            message=f"Unknown report type: {raw!r}",
            details={"valid_types": [rt.value for rt in ReportType]},
        ) from None


def _sse_format(event: StreamEvent) -> bytes:
    """Serialize a `StreamEvent` as one SSE frame.

    Each frame is `data: <json>\n\n` — the `\n\n` is the SSE protocol
    terminator that flushes one chunk to the browser. We don't set an
    `event:` field because every frame is the same event type; the
    SPA parses the JSON `kind` field instead.
    """
    import orjson

    payload = orjson.dumps(event.model_dump(mode="json")).decode("utf-8")
    return f"data: {payload}\n\n".encode()


# --------------------------------------------------------------------------
# POST /reports/{report_type}/stream — SSE
# --------------------------------------------------------------------------
@router.post(
    "/{report_type}/stream",
    summary="Stream a Claude narrative for the given report type.",
    responses={
        200: {
            "description": (
                "Server-Sent Events stream. Each frame is one "
                "`StreamEvent` (kind=run_meta | delta | final | error)."
            ),
        },
        404: {"description": "Unknown report type."},
        429: {"description": "Rate limit exceeded."},
        503: {
            "description": (
                "AI narrative generation is disabled "
                "(FEATURE_REPORTS_AI_NARRATIVE=false or CLAUDE_API_KEY unset)."
            ),
        },
    },
)
# Rate-limited per-IP. The slowapi decorator requires a literal limit
# string at decoration time, so the value in `settings.RATE_LIMIT_AI_NARRATIVE_PER_MINUTE`
# is the documented source of truth but the literal here is the
# enforcement. Keep them in sync — this module's value is the
# floor (5/min) used by the limiter; settings value is the same.
@limiter.limit("5/minute")
async def post_report_stream(
    request: Request,
    report_type: str,
    payload: ReportRunCreate,
    auth: CurrentAuth,
    session: Annotated[AsyncSession, Depends(get_session_with_auth)],
) -> StreamingResponse:
    """Stream a Claude narrative for the given report type.

    SSE frames are `data: <json>\n\n`. The first frame is always
    `kind='run_meta'` with the freshly-inserted `run_id`; subsequent
    frames are `kind='delta'` chunks of narrative text. The terminal
    frame is either `kind='final'` (with token count + cost) or
    `kind='error'` (with a short message — see logs for detail).
    """
    _reports_ai_enabled_or_503()
    rt = _parse_report_type(report_type)

    if auth.agency_id is None:
        # Agency-admin operations always require an agency scope.
        raise NotFoundError(message="No agency context for this user.")

    service = ReportService()

    async def event_iter() -> AsyncIterator[bytes]:
        try:
            async for event in service.generate(
                session,
                agency_id=auth.agency_id,
                user_id=auth.user_id,
                report_type=rt,
                params=payload.params.model_dump(exclude_none=True),
            ):
                yield _sse_format(event)
        except Exception as exc:  # pragma: no cover — last-resort guard
            # Anything that escapes `service.generate` (which has its
            # own try/except) becomes a graceful error frame instead
            # of tearing down the connection.
            log.error(
                "reports.stream_unhandled_exception",
                error=type(exc).__name__,
                detail=str(exc),
            )
            yield _sse_format(
                StreamEvent(kind="error", error="Internal error during streaming.")
            )

    return StreamingResponse(
        event_iter(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # disable nginx buffering if proxied
            "Connection": "keep-alive",
        },
    )


# --------------------------------------------------------------------------
# GET /reports/runs — history list
# --------------------------------------------------------------------------
@router.get(
    "/runs",
    response_model=list[ReportRunSummary],
    summary="List the agency's recent report runs.",
    responses=standard_responses(include=[401, 403]),
)
async def get_runs(
    auth: CurrentAuth,
    session: Annotated[AsyncSession, Depends(get_session_with_auth)],
    limit: int = Query(default=20, ge=1, le=100),
) -> list[ReportRunSummary]:
    if auth.agency_id is None:
        return []
    rows = await reports_service.list_runs(
        session, agency_id=auth.agency_id, limit=limit
    )
    return [
        ReportRunSummary(
            id=row.id,
            report_type=ReportType(row.report_type),
            status=row.status,  # type: ignore[arg-type]
            created_at=row.created_at,
            completed_at=row.completed_at,
            narrative_chars=len(row.narrative or ""),
        )
        for row in rows
    ]


# --------------------------------------------------------------------------
# GET /reports/runs/{run_id} — one run
# --------------------------------------------------------------------------
@router.get(
    "/runs/{run_id}",
    response_model=ReportRunRead,
    summary="Read one report run.",
    responses=standard_responses(include=[401, 403, 404]),
)
async def get_run(
    run_id: uuid.UUID,
    auth: CurrentAuth,
    session: Annotated[AsyncSession, Depends(get_session_with_auth)],
) -> ReportRunRead:
    if auth.agency_id is None:
        raise NotFoundError(message="Run not found.")
    run = await reports_service.get_run(
        session, run_id=run_id, agency_id=auth.agency_id
    )
    if run is None:
        raise NotFoundError(message="Run not found.")
    return ReportRunRead.model_validate(run)


# --------------------------------------------------------------------------
# GET /reports/runs/{run_id}/export — PDF / CSV / XLSX
# --------------------------------------------------------------------------
@router.get(
    "/runs/{run_id}/export",
    summary="Export a completed report run as PDF, CSV, or XLSX.",
    responses={
        200: {
            "description": (
                "Binary file (application/pdf, text/csv, or "
                "application/vnd.openxmlformats-officedocument.spreadsheetml.xlsx) "
                "with `Content-Disposition: attachment`."
            ),
        },
        404: {"description": "Run not found."},
        422: {"description": "Unsupported format."},
    },
)
async def get_run_export(
    run_id: uuid.UUID,
    auth: CurrentAuth,
    session: Annotated[AsyncSession, Depends(get_session_with_auth)],
    format: str = Query(default="pdf", pattern="^(pdf|csv|xlsx)$"),
) -> StreamingResponse:
    if auth.agency_id is None:
        raise NotFoundError(message="Run not found.")
    run = await reports_service.get_run(
        session, run_id=run_id, agency_id=auth.agency_id
    )
    if run is None:
        raise NotFoundError(message="Run not found.")

    blob = reports_service.get_artifact(run, fmt=format)
    content_type = {
        "pdf": "application/pdf",
        "csv": "text/csv; charset=utf-8",
        "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.xlsx",
    }[format]
    filename = f"report-{run.report_type}-{run.id}.{format}"

    async def _iter() -> AsyncIterator[bytes]:
        yield blob

    return StreamingResponse(
        _iter(),
        media_type=content_type,
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Content-Length": str(len(blob)),
        },
    )


__all__ = ["router"]
