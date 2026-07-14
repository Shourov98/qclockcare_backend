"""Agencies service — business logic for SUPER_ADMIN agency management.

Cross-agency reads (the SUPER_ADMIN list endpoint) bypass the RLS policy
that scopes by `app.current_agency_id` by using a service role connection.
For per-agency reads, we still pass `agency_id` explicitly so the service
is auditable from logs.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.exceptions import ConflictError, NotFoundError, ValidationError
from src.modules.agencies.models import Agency, AgencyProgram, Program
from src.modules.agencies.schemas import (
    AgencyCreateRequest,
    AgencyUpdateRequest,
)
from src.shared.domain.enums import ProgramType


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
async def _get_agency_or_404(
    session: AsyncSession,
    *,
    agency_id: uuid.UUID,
    include_deleted: bool = False,
) -> Agency:
    """Fetch one agency by id.

    Args:
        include_deleted: when True, soft-deleted rows are returned
            (used by the PATCH endpoint to allow restoring a row).

    Raises:
        NotFoundError: if not found.
    """
    stmt = select(Agency).where(Agency.id == agency_id)
    if not include_deleted:
        stmt = stmt.where(Agency.deleted_at.is_(None))
    agency = (await session.execute(stmt)).scalar_one_or_none()
    if agency is None:
        raise NotFoundError(details={"resource": "agency", "id": str(agency_id)})
    return agency


async def _resolve_program_codes(
    session: AsyncSession,
    codes: list[str],
) -> list[Program]:
    """Look up Program rows by their code enum value.

    Raises:
        NotFoundError: if any code doesn't resolve (shouldn't happen
            if Pydantic validation passed — defence in depth).
    """
    if not codes:
        return []
    rows = (await session.execute(select(Program).where(Program.code.in_(codes)))).scalars().all()
    found_codes = {r.code.value for r in rows}
    missing = [c for c in codes if c not in found_codes]
    if missing:
        # Pydantic validator catches this first; treat as 404 if it
        # somehow slips through (e.g. a code was removed between
        # validation and lookup).
        raise NotFoundError(details={"resource": "program", "codes": missing})
    return list(rows)


# --------------------------------------------------------------------------
# Reads
# --------------------------------------------------------------------------
async def list_agencies(
    session: AsyncSession,
    *,
    page: int,
    page_size: int,
    include_deleted: bool = False,
    status_filter: str | None = None,
) -> tuple[list[Agency], int]:
    """List all agencies (SUPER_ADMIN only — bypasses RLS scoping).

    Filters:
      - include_deleted: include soft-deleted rows
      - status_filter:   narrow to one AgencyStatus value

    Returns (rows, total).
    """
    base = select(Agency)
    if not include_deleted:
        base = base.where(Agency.deleted_at.is_(None))
    if status_filter is not None:
        base = base.where(Agency.status == status_filter)

    total = (await session.execute(select(func.count()).select_from(base.subquery()))).scalar_one()

    offset = (page - 1) * page_size
    rows = (
        (
            await session.execute(
                base.order_by(Agency.name, Agency.id).offset(offset).limit(page_size)
            )
        )
        .scalars()
        .all()
    )
    return list(rows), int(total)


async def get_agency(
    session: AsyncSession,
    *,
    agency_id: uuid.UUID,
) -> Agency:
    """Fetch one active agency (raises NotFoundError if missing or deleted)."""
    return await _get_agency_or_404(session, agency_id=agency_id, include_deleted=False)


# --------------------------------------------------------------------------
# Writes
# --------------------------------------------------------------------------
async def create_agency(
    session: AsyncSession,
    *,
    payload: AgencyCreateRequest,
) -> Agency:
    """Insert one new agency + (optionally) attach programs.

    `status` starts at ACTIVE per the checklist (4.2.1).
    `initial_program_codes` is best-effort: unknown codes surface as
    422 from the schema layer before we reach here.
    """
    agency = Agency(
        name=payload.name,
        timezone=payload.timezone,
        settings=payload.settings,
    )
    session.add(agency)
    try:
        await session.flush()
    except IntegrityError as exc:
        # Name is not unique in the schema (intentional — multiple agencies
        # could share a name); this branch is a defence-in-depth for any
        # future unique constraint we add.
        raise ConflictError(
            message="Agency creation violated a uniqueness constraint.",
            details={"constraint": str(getattr(exc, "orig", exc))},
        ) from exc

    if payload.initial_program_codes:
        programs = await _resolve_program_codes(session, payload.initial_program_codes)
        for program in programs:
            session.add(
                AgencyProgram(
                    agency_id=agency.id,
                    program_id=program.id,
                    is_enabled=True,
                )
            )
        await session.flush()
    return agency


async def update_agency(
    session: AsyncSession,
    *,
    agency_id: uuid.UUID,
    payload: AgencyUpdateRequest,
) -> Agency:
    """Apply a partial update to one agency.

    Only fields explicitly set on `payload` are written (None vs
    "not provided" is distinguished by `model_fields_set`).
    """
    agency = await _get_agency_or_404(session, agency_id=agency_id, include_deleted=False)
    updates = payload.model_dump(exclude_unset=True)

    # If the caller explicitly wants to set status to SUSPENDED or CHURNED,
    # document it via settings.suspended_at / churned_at so audit log readers
    # have a timestamp to work with.
    new_status = updates.get("status")
    now = datetime.now(UTC)
    if new_status == "SUSPENDED" and "settings" not in updates:
        settings = dict(agency.settings or {})
        settings.setdefault("suspended_at", now.isoformat())
        updates["settings"] = settings
    elif new_status == "CHURNED" and "settings" not in updates:
        settings = dict(agency.settings or {})
        settings.setdefault("churned_at", now.isoformat())
        updates["settings"] = settings
    elif new_status in {"ACTIVE", "TRIAL"} and "settings" not in updates:
        # Clear any previously-set suspension flag so a reactivation is
        # tracked.
        settings = dict(agency.settings or {})
        if "suspended_at" in settings:
            settings["reactivated_at"] = now.isoformat()
            del settings["suspended_at"]
        if "churned_at" in settings:
            del settings["churned_at"]
        updates["settings"] = settings

    for field, value in updates.items():
        setattr(agency, field, value)
    await session.flush()
    return agency


async def soft_delete_agency(
    session: AsyncSession,
    *,
    agency_id: uuid.UUID,
) -> Agency:
    """Mark the agency as deleted (preserves history for FK references).

    Idempotent: deleting an already-deleted row returns the same row
    without re-stamping `deleted_at`.

    Note: agencies own user_roles, staff, patients, etc. via CASCADE.
    The soft-delete does NOT cascade — those rows stay alive (their
    `agency_id` FK references the agency even after deletion). If you
    need to physically wipe the agency's data, run a separate cleanup
    operation (out of scope for this endpoint).
    """
    agency = await _get_agency_or_404(session, agency_id=agency_id, include_deleted=True)
    if agency.deleted_at is None:
        agency.deleted_at = datetime.now(UTC)
    await session.flush()
    return agency


# --------------------------------------------------------------------------
# Programs sub-resource
# --------------------------------------------------------------------------
async def list_agency_programs(
    session: AsyncSession,
    *,
    agency_id: uuid.UUID,
) -> list[tuple[AgencyProgram, Program]]:
    """Return (agency_program, program) pairs for the agency.

    Verifies the agency exists first (so an unknown agency_id returns
    404, not an empty list).
    """
    await _get_agency_or_404(session, agency_id=agency_id, include_deleted=True)
    stmt = (
        select(AgencyProgram, Program)
        .join(Program, Program.id == AgencyProgram.program_id)
        .where(AgencyProgram.agency_id == agency_id)
        .order_by(Program.code)
    )
    rows = (await session.execute(stmt)).all()
    return [(ap, p) for ap, p in rows]


async def set_agency_program(
    session: AsyncSession,
    *,
    agency_id: uuid.UUID,
    program_code: str,
    is_enabled: bool,
) -> AgencyProgram:
    """Create or update the (agency, program) row.

    Used by a future `PUT /agencies/{id}/programs/{code}` endpoint
    (not yet exposed — kept here for service completeness).
    """
    await _get_agency_or_404(session, agency_id=agency_id, include_deleted=False)
    if program_code not in {pt.value for pt in ProgramType}:
        raise ValidationError(
            f"unknown program code: {program_code}",
            details={"valid_codes": sorted(pt.value for pt in ProgramType)},
        )
    program = (
        await session.execute(select(Program).where(Program.code == program_code))
    ).scalar_one_or_none()
    if program is None:
        raise NotFoundError(details={"resource": "program", "code": program_code})
    existing = (
        await session.execute(
            select(AgencyProgram).where(
                AgencyProgram.agency_id == agency_id,
                AgencyProgram.program_id == program.id,
            )
        )
    ).scalar_one_or_none()
    if existing is None:
        ap = AgencyProgram(
            agency_id=agency_id,
            program_id=program.id,
            is_enabled=is_enabled,
        )
        session.add(ap)
    else:
        existing.is_enabled = is_enabled
        ap = existing
    await session.flush()
    return ap


# --------------------------------------------------------------------------
# Imports placed below to avoid a circular import with enums.
# --------------------------------------------------------------------------
__all__ = [
    "create_agency",
    "get_agency",
    "list_agencies",
    "list_agency_programs",
    "set_agency_program",
    "soft_delete_agency",
    "update_agency",
]
