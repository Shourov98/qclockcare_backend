"""AuditLog ORM model.

Append-only. UPDATE/DELETE are blocked by a DB trigger (`audit_logs_no_modify`).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import INET, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.shared.domain.base_entity import Base, IdMixin
from src.shared.domain.enum_mapping import pg_name
from src.shared.domain.enums import AuditAction


class AuditLog(IdMixin, Base):
    """One row per logical business action.

    `old_data` / `new_data` carry the before/after snapshots for UPDATE
    actions; CREATE actions have only `new_data`; DELETE actions have
    only `old_data`. Free-form jsonb so each module can encode its own
    shape (the project doesn't enforce a schema here — fields are
    stable per `entity_type`, not globally).
    """

    __tablename__ = "audit_logs"

    agency_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("agencies.id", ondelete="CASCADE"),
        nullable=True,
    )
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    action: Mapped[AuditAction] = mapped_column(
        Enum(AuditAction, name=pg_name(AuditAction)),
        nullable=False,
    )
    entity_type: Mapped[str] = mapped_column(Text, nullable=False)
    entity_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    old_data: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    new_data: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    metadata_: Mapped[dict[str, Any]] = mapped_column(
        "metadata",
        JSONB,
        nullable=False,
        default=dict,
        server_default=text("'{}'::jsonb"),
    )
    ip_address: Mapped[str | None] = mapped_column(INET, nullable=True)
    user_agent: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
    )

    __table_args__ = (
        Index(
            "idx_audit_logs_agency_date",
            "agency_id",
            text("created_at DESC"),
        ),
        Index(
            "idx_audit_logs_actor",
            "actor_user_id",
            text("created_at DESC"),
        ),
        Index(
            "idx_audit_logs_entity",
            "entity_type",
            "entity_id",
            text("created_at DESC"),
        ),
        Index(
            "idx_audit_logs_action",
            "action",
            text("created_at DESC"),
        ),
        CheckConstraint(
            "entity_type <> '' AND length(trim(entity_type)) > 0",
            name="ck_audit_logs_entity_type_non_empty",
        ),
    )


__all__ = ["AuditLog"]
