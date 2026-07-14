"""Patient/Guardian portal service — verifies a caller is linked to a visit
before delegating to the visits module.

Every function in this module:
  1. Resolves the caller's `user_id` to either a PatientProfile or a
     GuardianProfile within `ctx.agency_id`.
  2. For GUARDIAN callers, requires an active `is_legal=true` relationship
     to the visit's patient (valid_until NULL or >= today).
  3. Verifies the resolved patient matches `visit.appointment.patient_id`.
  4. Delegates the actual read/write to the visits module.

The relationship check is intentionally re-implemented at the service
layer (not just RLS) so we can return a 403 with a clear error code
("not linked", "relationship expired") rather than a generic RLS 404.
"""

from __future__ import annotations

import uuid
from datetime import date

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.exceptions import ForbiddenError, NotFoundError
from src.modules.identity.dependencies import AuthContext
from src.modules.patients.models import (
    GuardianProfile,
    PatientGuardianRelationship,
    PatientProfile,
)
from src.modules.visits import service as visits_service
from src.modules.visits.models import ServiceVerification, Visit, VisitIssue
from src.modules.visits.schemas import (
    DisputeReasonCode as _DisputeReasonCode,
)
from src.modules.visits.schemas import (
    ServiceVerificationCreateRequest,
    VisitIssueCreateRequest,
)
from src.modules.visits.schemas import (
    VerificationStatus as _VerificationStatus,
)
from src.shared.domain.enums import UserRole


# --------------------------------------------------------------------------
# Resolver helpers
# --------------------------------------------------------------------------
async def _resolve_patient_for_caller(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    agency_id: uuid.UUID,
) -> PatientProfile | None:
    """Return the PatientProfile owned by `user_id` in `agency_id`, or None."""
    return (
        await session.execute(
            select(PatientProfile).where(
                PatientProfile.user_id == user_id,
                PatientProfile.agency_id == agency_id,
            )
        )
    ).scalar_one_or_none()


async def _resolve_guardian_for_caller(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    agency_id: uuid.UUID,
) -> GuardianProfile | None:
    return (
        await session.execute(
            select(GuardianProfile).where(
                GuardianProfile.user_id == user_id,
                GuardianProfile.agency_id == agency_id,
            )
        )
    ).scalar_one_or_none()


async def _assert_guardian_linked_to_patient(
    session: AsyncSession,
    *,
    guardian: GuardianProfile,
    patient_id: uuid.UUID,
    agency_id: uuid.UUID,
) -> None:
    """Raise ForbiddenError if `guardian` lacks an active legal link to `patient_id`.

    "Active" = is_legal=true AND (valid_until IS NULL OR valid_until >= today).
    Multiple relationship types are allowed (PARENT + GUARDIAN, etc.) — any
    one active legal relationship is sufficient.
    """
    today = date.today()
    rows = (
        await session.execute(
            select(PatientGuardianRelationship).where(
                PatientGuardianRelationship.guardian_id == guardian.id,
                PatientGuardianRelationship.patient_id == patient_id,
                PatientGuardianRelationship.agency_id == agency_id,
                PatientGuardianRelationship.is_legal.is_(True),
            )
        )
    ).scalars().all()
    if not rows:
        raise ForbiddenError(
            "Guardian is not linked to this patient.",
            details={"reason": "no_legal_relationship"},
        )
    active = [r for r in rows if r.valid_until is None or r.valid_until >= today]
    if not active:
        raise ForbiddenError(
            "Guardian relationship has expired.",
            details={"reason": "relationship_expired"},
        )


async def _resolve_caller_to_patients(
    session: AsyncSession,
    *,
    ctx: AuthContext,
) -> set[uuid.UUID]:
    """Return the set of patient ids the caller is allowed to act for.

    For PATIENT: exactly one (their own).
    For GUARDIAN: every patient they have an active legal relationship to.
    """
    if ctx.agency_id is None:
        return set()
    if ctx.role == UserRole.PATIENT:
        patient = await _resolve_patient_for_caller(
            session, user_id=ctx.user_id, agency_id=ctx.agency_id
        )
        if patient is None:
            return set()
        return {patient.id}
    if ctx.role == UserRole.GUARDIAN:
        guardian = await _resolve_guardian_for_caller(
            session, user_id=ctx.user_id, agency_id=ctx.agency_id
        )
        if guardian is None:
            return set()
        today = date.today()
        rels = (
            await session.execute(
                select(PatientGuardianRelationship).where(
                    PatientGuardianRelationship.guardian_id == guardian.id,
                    PatientGuardianRelationship.agency_id == ctx.agency_id,
                    PatientGuardianRelationship.is_legal.is_(True),
                )
            )
        ).scalars().all()
        return {
            r.patient_id
            for r in rels
            if r.valid_until is None or r.valid_until >= today
        }
    raise ForbiddenError(
        "Portal endpoints are only available to PATIENT or GUARDIAN.",
        details={"role": ctx.role.value},
    )


async def _load_visit_for_caller(
    session: AsyncSession,
    *,
    visit_id: uuid.UUID,
    ctx: AuthContext,
) -> Visit:
    """Load the visit and verify the caller is allowed to see it.

    The visits module's `_get_visit_or_404` already scopes by agency_id.
    We additionally verify patient/guardian linkage here so we can return
    a clear 403 (vs. a misleading RLS 404).
    """
    if ctx.agency_id is None:
        raise NotFoundError("Visit not found.")
    visit = await visits_service._get_visit_or_404(
        session, visit_id=visit_id, agency_id=ctx.agency_id
    )
    appointment = await visits_service._get_appointment_or_404(
        session, appointment_id=visit.appointment_id, agency_id=ctx.agency_id
    )

    if ctx.role == UserRole.PATIENT:
        patient = await _resolve_patient_for_caller(
            session, user_id=ctx.user_id, agency_id=ctx.agency_id
        )
        if patient is None:
            raise NotFoundError("Visit not found.")
        if patient.id != appointment.patient_id:
            # A patient at this agency should not be able to see another
            # patient's visit — return 404 to avoid leaking visit existence.
            raise NotFoundError("Visit not found.")
        return visit

    if ctx.role == UserRole.GUARDIAN:
        guardian = await _resolve_guardian_for_caller(
            session, user_id=ctx.user_id, agency_id=ctx.agency_id
        )
        if guardian is None:
            raise NotFoundError("Visit not found.")
        await _assert_guardian_linked_to_patient(
            session,
            guardian=guardian,
            patient_id=appointment.patient_id,
            agency_id=ctx.agency_id,
        )
        return visit

    raise ForbiddenError(
        "Portal endpoints are only available to PATIENT or GUARDIAN.",
        details={"role": ctx.role.value},
    )


async def load_visit_with_relations(
    session: AsyncSession,
    *,
    visit_id: uuid.UUID,
    ctx: AuthContext,
) -> Visit:
    """Load a visit (after authz check) with its nested children eager-loaded."""
    visit = await _load_visit_for_caller(
        session, visit_id=visit_id, ctx=ctx
    )
    await session.refresh(
        visit,
        attribute_names=["service_items", "notes", "verification", "issues"],
    )
    return visit


# --------------------------------------------------------------------------
# List / get
# --------------------------------------------------------------------------
async def list_my_visits(
    session: AsyncSession,
    *,
    ctx: AuthContext,
    limit: int,
    offset: int,
) -> list[Visit]:
    """Return visits the caller is allowed to see, newest first.

    For a PATIENT caller, this is the visits where the appointment's
    patient is them.
    For a GUARDIAN caller, this is the union across all patients they
    have an active legal relationship to.
    """
    patient_ids = await _resolve_caller_to_patients(session, ctx=ctx)
    if not patient_ids:
        return []
    if len(patient_ids) == 1:
        rows, _ = await visits_service.list_visits(
            session,
            agency_id=ctx.agency_id,  # type: ignore[arg-type]
            patient_id=next(iter(patient_ids)),
            page=(offset // max(1, limit)) + 1,
            page_size=limit,
        )
        return list(rows)
    # Multiple patients — fetch each, sort merged result by check_in_time desc.
    all_rows: list[Visit] = []
    for pid in patient_ids:
        rows, _ = await visits_service.list_visits(
            session,
            agency_id=ctx.agency_id,  # type: ignore[arg-type]
            patient_id=pid,
            page=1,
            page_size=limit,
        )
        all_rows.extend(rows)
    # Sort newest first (use check_in_time desc, then id desc for stability).
    all_rows.sort(
        key=lambda v: (v.check_in_time or v.created_at, v.id),
        reverse=True,
    )
    # Apply offset/limit in Python — acceptable for the portal's expected
    # page sizes (<= 100 per page across a handful of dependents).
    return all_rows[offset : offset + limit]


# --------------------------------------------------------------------------
# Verify / dispute / report-issue
# --------------------------------------------------------------------------
async def verify_visit(
    session: AsyncSession,
    *,
    visit_id: uuid.UUID,
    ctx: AuthContext,
    comment: str | None,
) -> ServiceVerification:
    """File a positive verification (idempotent — updates an existing row)."""
    visit = await _load_visit_for_caller(
        session, visit_id=visit_id, ctx=ctx
    )
    payload = ServiceVerificationCreateRequest(
        status=_VerificationStatus.VERIFIED,
        dispute_reason_code=None,
        comment=comment,
    )
    return await visits_service.get_or_create_verification(
        session,
        visit_id=visit.id,
        agency_id=visit.agency_id,
        verified_by=ctx.user_id,
        verifier_role=ctx.role,
        payload=payload,
    )


async def dispute_visit(
    session: AsyncSession,
    *,
    visit_id: uuid.UUID,
    ctx: AuthContext,
    dispute_reason_code: str,
    comment: str | None,
) -> ServiceVerification:
    """File a dispute (idempotent — updates an existing row)."""
    try:
        reason = _DisputeReasonCode(dispute_reason_code)
    except ValueError as exc:
        raise NotFoundError(
            "Unknown dispute reason code.",
            details={"code": dispute_reason_code},
        ) from exc
    visit = await _load_visit_for_caller(
        session, visit_id=visit_id, ctx=ctx
    )
    payload = ServiceVerificationCreateRequest(
        status=_VerificationStatus.DISPUTED,
        dispute_reason_code=reason,
        comment=comment,
    )
    return await visits_service.get_or_create_verification(
        session,
        visit_id=visit.id,
        agency_id=visit.agency_id,
        verified_by=ctx.user_id,
        verifier_role=ctx.role,
        payload=payload,
    )


async def report_issue(
    session: AsyncSession,
    *,
    visit_id: uuid.UUID,
    ctx: AuthContext,
    issue_type: str,
    comment: str,
) -> VisitIssue:
    visit = await _load_visit_for_caller(
        session, visit_id=visit_id, ctx=ctx
    )
    payload = VisitIssueCreateRequest(issue_type=issue_type, comment=comment)
    return await visits_service.add_visit_issue(
        session,
        visit_id=visit.id,
        agency_id=visit.agency_id,
        payload=payload,
        reported_by_user_id=ctx.user_id,
    )


__all__ = [
    "dispute_visit",
    "list_my_visits",
    "load_visit_with_relations",
    "report_issue",
    "verify_visit",
]
