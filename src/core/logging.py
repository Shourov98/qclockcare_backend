"""Structured logging via structlog.

JSON in prod, human-readable in dev. Every log line includes the request_id
when one is available (set by middleware).
"""

from __future__ import annotations

import logging
import sys
from typing import Any

import structlog
from structlog.types import EventDict, Processor

from src.core.config import settings


def _add_app_metadata(_: Any, method_name: str, event_dict: EventDict) -> EventDict:
    """Inject app name, version, and env into every log record."""
    event_dict.setdefault("app", settings.APP_NAME)
    event_dict.setdefault("version", settings.APP_VERSION)
    event_dict.setdefault("env", settings.APP_ENV)
    return event_dict


def _rename_event_to_message(_: Any, method_name: str, event_dict: EventDict) -> EventDict:
    """structlog uses 'event'; we'll alias it to 'message' for friendlier output."""
    if "event" in event_dict and "message" not in event_dict:
        event_dict["message"] = event_dict["event"]
    return event_dict


def configure_logging() -> None:
    """Initialize structlog + stdlib logging.

    Idempotent — safe to call multiple times (e.g. from tests).
    """
    shared_processors: list[Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        _add_app_metadata,
        _rename_event_to_message,
        structlog.processors.StackInfoRenderer(),
    ]

    if settings.LOG_FORMAT == "json":
        # Production: one JSON object per line, ready for CloudWatch / Loki / Datadog
        renderer: Processor = structlog.processors.JSONRenderer()
    else:
        # Development: pretty console output
        renderer = structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty())

    structlog.configure(
        processors=[*shared_processors, renderer],
        wrapper_class=structlog.make_filtering_bound_logger(getattr(logging, settings.LOG_LEVEL)),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(file=sys.stdout),
        cache_logger_on_first_use=True,
    )

    # Bridge stdlib logging (e.g. uvicorn, sqlalchemy) into structlog
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=getattr(logging, settings.LOG_LEVEL),
    )

    # Quiet down noisy libraries
    for noisy in ("botocore", "boto3", "s3transfer", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def get_logger(name: str | None = None) -> Any:
    """Get a structlog logger. Use `logger.info("msg", key=value)`.

    Return type is Any because structlog's BoundLogger protocol isn't fully
    typed in the stdlib stub.
    """
    return structlog.get_logger(name)


__all__ = ["configure_logging", "get_logger"]
