"""Appointment events helper — append-only timeline writer.

The `append_appointment_event` helper is the single insertion path for
`appointment_events` rows. It is called by the appointment service layer
during state transitions and lifecycle actions. The DB trigger
(`trg_appointment_events_no_modify`) blocks UPDATE/DELETE — this helper
only inserts.

For audit-grade actor tracking (IP, user-agent) the helper takes the
optional values from the router layer.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from src.modules.appointments.models import AppointmentEvent
from src.shared.domain.enums import AppointmentEventType, AppointmentStatus


async def append_appointment_event(
    session: AsyncSession,
    *,
    agency_id: uuid.UUID,
    appointment_id: uuid.UUID,
    event_type: AppointmentEventType,
    actor_user_id: uuid.UUID | None = None,
    from_status: AppointmentStatus | None = None,
    to_status: AppointmentStatus | None = None,
    metadata: dict[str, Any] | None = None,
    ip_address: str | None = None,
    user_agent: str | None = None,
) -> AppointmentEvent:
    """Append one row to `appointment_events`.

    Caller is responsible for committing the surrounding transaction.
    Raises on constraint violations (FK to appointments/agencies/users)
    so the caller can choose to surface the error or swallow it for
    best-effort logging.
    """
    row = AppointmentEvent(
        agency_id=agency_id,
        appointment_id=appointment_id,
        actor_user_id=actor_user_id,
        event_type=event_type,
        from_status=from_status,
        to_status=to_status,
        metadata_=metadata or {},
        ip_address=ip_address,
        user_agent=user_agent,
    )
    session.add(row)
    await session.flush()
    return row


__all__ = ["append_appointment_event"]
