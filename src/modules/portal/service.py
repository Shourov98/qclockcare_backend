"""Patient/Guardian portal service — verifies a caller is linked to a visit
before delegating to the visits module.

The portal is a read-only surface. Every function here:
  1. Resolves the caller's `user_id` to either a PatientProfile or a
     GuardianProfile within `ctx.agency_id`.
  2. For GUARDIAN callers, requires an active `is_legal=true` relationship
     to the visit's patient (valid_until NULL or >= today).
  3. Verifies the resolved patient matches `visit.appointment.patient_id`.
  4. Delegates the actual read to the visits module.

The relationship check is intentionally re-implemented at the service
layer (not just RLS) so we can return a 403 with a clear error code
("not linked", "relationship expired") rather than a generic RLS 404.

The spec's signature flow (`POST /visits/{id}/sign`) lives on the
visits router — the portal doesn't expose its own /sign endpoint.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from src.core.exceptions import ForbiddenError, NotFoundError
from src.modules.compliance.issues import ComplianceIssue
from src.modules.compliance.models import AgencyDocument, AgencyLicense
from src.modules.identity.dependencies import AuthContext
from src.modules.patients.models import (
    GuardianProfile,
    PatientGuardianRelationship,
    PatientProfile,
)
from src.modules.portal.schemas import (
    PortalComplianceRecentActivity,
    PortalComplianceResponse,
    PortalComplianceSubScore,
    PortalComplianceUpcomingAudit,
    PortalComplianceUrgentAction,
)
from src.modules.visits import service as visits_service
from src.modules.visits.models import Visit
from src.shared.domain.enums import (
    ComplianceIssueSeverity,
    ComplianceIssueStatus,
    DocumentStatus,
    LicenseStatus,
    UserRole,
)
from src.shared.utils.datetime_utils import utc_now


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
    """Load a visit (after authz check) with its nested children + joined
    caregiver / patient info eager-loaded.

    Eager-loads mirror the staff-side `get_visit` shape so the portal can
    render the same Visit Summary card. Per spec §8 the signature row is
    the source-of-truth for "did the patient sign"; per spec §10 the
    EVV start + end records live on the `evv_records` sibling table.
    """
    visit = await _load_visit_for_caller(
        session, visit_id=visit_id, ctx=ctx
    )
    # Re-load with the joined chains in one round trip.
    from src.modules.appointments.models import Appointment
    from src.modules.patients.models import PatientProfile
    from src.modules.staff.models import StaffProfile
    from src.modules.visits.models import VisitActivityDelivery

    reloaded = (
        await session.execute(
            select(Visit)
            .where(Visit.id == visit.id)
            .options(
                selectinload(Visit.activity_deliveries).selectinload(
                    VisitActivityDelivery.activity
                ),
                selectinload(Visit.notes),
                selectinload(Visit.signature),
                selectinload(Visit.evv_record),
                # Caregiver: Visit.staff -> StaffProfile.user
                selectinload(Visit.staff).selectinload(StaffProfile.user),
                # Patient + location: Visit.appointment -> Appointment
                #   -> PatientProfile.user
                selectinload(Visit.appointment)
                .selectinload(Appointment.patient)
                .selectinload(PatientProfile.user),
            )
        )
    ).scalar_one_or_none()
    return reloaded if reloaded is not None else visit


# --------------------------------------------------------------------------
# List
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
    # Multiple patients — fetch each, sort merged result by visit date desc.
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
    # Sort newest first (use appointment.scheduled_start desc, then id).
    all_rows.sort(
        key=lambda v: (
            getattr(getattr(v, "appointment", None), "scheduled_start", None)
            or v.created_at,
            v.id,
        ),
        reverse=True,
    )
    # Apply offset/limit in Python — acceptable for the portal's expected
    # page sizes (<= 100 per page across a handful of dependents).
    return all_rows[offset : offset + limit]


# --------------------------------------------------------------------------
# Compliance dashboard
# --------------------------------------------------------------------------
def _bucket_color(percent: int) -> str:
    """Mirror the FE tailwind thresholds: green ≥ 90, orange 70-89, red < 70."""
    if percent >= 90:
        return "green"
    if percent >= 70:
        return "orange"
    return "red"


def _relative_due_label(due_at: datetime | None, *, now: datetime) -> str:
    """Render the FE-friendly due label ("Overdue by 1 day", "2 days", …)."""
    if due_at is None:
        return "—"
    if due_at < now:
        days = (now - due_at).days
        return f"Overdue by {days} day{'s' if days != 1 else ''}"
    days = (due_at - now).days
    if days == 0:
        return "Today"
    if days == 1:
        return "1 day"
    return f"{days} days"


async def get_portal_compliance(
    session: AsyncSession,
    *,
    ctx: AuthContext,
) -> PortalComplianceResponse:
    """Compute the compliance dashboard for the calling patient/guardian.

    All counts come from live queries against `agency_documents`,
    `agency_licenses`, and `compliance_issues` for the caller's agency.
    RLS narrows PATIENT/GUARDIAN to the agency they're logged into; the
    `_resolve_caller_to_patients` helper above is used to short-circuit
    empty calls without an agency context.
    """
    if ctx.role not in {UserRole.PATIENT, UserRole.GUARDIAN}:
        raise ForbiddenError(
            "Portal endpoints are only available to PATIENT or GUARDIAN.",
            details={"role": ctx.role.value},
        )
    # Mirror the other portal endpoints: 404 if the caller has no
    # resolvable patient/guardian profile.
    patient_ids = await _resolve_caller_to_patients(session, ctx=ctx)
    if not patient_ids:
        raise NotFoundError(
            "No linked patient profile found.",
            details={"reason": "no_profile"},
        )
    if ctx.agency_id is None:
        raise NotFoundError(
            "Caller has no agency context.",
            details={"reason": "no_agency"},
        )
    agency_id = ctx.agency_id
    now = utc_now()

    # ----- Document counts (Documentation sub-score) -----
    doc_total = (
        await session.execute(
            select(func.count())
            .select_from(AgencyDocument)
            .where(
                AgencyDocument.agency_id == agency_id,
                AgencyDocument.deleted_at.is_(None),
            )
        )
    ).scalar_one()
    doc_valid_total = (
        await session.execute(
            select(func.count())
            .select_from(AgencyDocument)
            .where(
                AgencyDocument.agency_id == agency_id,
                AgencyDocument.deleted_at.is_(None),
                AgencyDocument.status.in_(
                    [DocumentStatus.VALID, DocumentStatus.EXPIRING]
                ),
            )
        )
    ).scalar_one()
    documentation_pct = (
        int(round((doc_valid_total / doc_total) * 100))
        if doc_total > 0
        else 100  # no docs required → trivially compliant
    )

    # ----- License counts (Staff Training sub-score) -----
    lic_total = (
        await session.execute(
            select(func.count())
            .select_from(AgencyLicense)
            .where(
                AgencyLicense.agency_id == agency_id,
                AgencyLicense.deleted_at.is_(None),
            )
        )
    ).scalar_one()
    lic_valid_total = (
        await session.execute(
            select(func.count())
            .select_from(AgencyLicense)
            .where(
                AgencyLicense.agency_id == agency_id,
                AgencyLicense.deleted_at.is_(None),
                AgencyLicense.status.in_(
                    [LicenseStatus.VALID, LicenseStatus.UPCOMING]
                ),
            )
        )
    ).scalar_one()
    staff_training_pct = (
        int(round((lic_valid_total / lic_total) * 100))
        if lic_total > 0
        else 100
    )

    # ----- Open issues count (Service Auth sub-score, placeholder) -----
    open_issue_total = (
        await session.execute(
            select(func.count())
            .select_from(ComplianceIssue)
            .where(
                ComplianceIssue.agency_id == agency_id,
                ComplianceIssue.deleted_at.is_(None),
                ComplianceIssue.status.notin_(
                    [
                        ComplianceIssueStatus.RESOLVED,
                        ComplianceIssueStatus.DISMISSED,
                    ]
                ),
            )
        )
    ).scalar_one()
    # Inverted score — fewer open issues = higher percent. We treat the
    # "service auth" surface as a placeholder until that table ships.
    # Cap the impact at 20 open issues (so 0 → 100, 20+ → 0).
    service_auth_pct = max(0, 100 - min(open_issue_total, 20) * 5)

    overall_pct = int(
        round(documentation_pct * 0.5 + staff_training_pct * 0.3 + service_auth_pct * 0.2)
    )
    overall_pct = max(0, min(100, overall_pct))

    sub_scores = [
        PortalComplianceSubScore(
            key="documentation",
            label="Documentation",
            percent=documentation_pct,
            color=_bucket_color(documentation_pct),
        ),
        PortalComplianceSubScore(
            key="staff_training",
            label="Staff Training",
            percent=staff_training_pct,
            color=_bucket_color(staff_training_pct),
        ),
        PortalComplianceSubScore(
            key="service_auth",
            label="Service Auth",
            percent=service_auth_pct,
            color=_bucket_color(service_auth_pct),
        ),
    ]

    # ----- Urgent actions: top 5 critical/high unresolved issues -----
    urgent_rows = (
        await session.execute(
            select(ComplianceIssue)
            .where(
                ComplianceIssue.agency_id == agency_id,
                ComplianceIssue.deleted_at.is_(None),
                ComplianceIssue.status.notin_(
                    [
                        ComplianceIssueStatus.RESOLVED,
                        ComplianceIssueStatus.DISMISSED,
                    ]
                ),
                ComplianceIssue.severity.in_(
                    [
                        ComplianceIssueSeverity.CRITICAL,
                        ComplianceIssueSeverity.HIGH,
                    ]
                ),
            )
            .order_by(ComplianceIssue.due_at.asc().nulls_last(), ComplianceIssue.created_at.desc())
            .limit(5)
        )
    ).scalars().all()
    urgent_actions = [
        PortalComplianceUrgentAction(
            id=row.id,
            title=row.title,
            description=row.description,
            due_label=_relative_due_label(row.due_at, now=now),
            severity=row.severity.value.lower(),
        )
        for row in urgent_rows
    ]

    # ----- Upcoming audits: expiring documents + licenses in next 60d -----
    upcoming_cutoff = now + timedelta(days=60)
    upcoming_docs = (
        await session.execute(
            select(AgencyDocument)
            .where(
                AgencyDocument.agency_id == agency_id,
                AgencyDocument.deleted_at.is_(None),
                AgencyDocument.expires_at.is_not(None),
                AgencyDocument.expires_at <= upcoming_cutoff,
                AgencyDocument.expires_at >= now,
            )
            .order_by(AgencyDocument.expires_at.asc())
            .limit(5)
        )
    ).scalars().all()
    upcoming_audits = [
        PortalComplianceUpcomingAudit(
            id=d.id,
            title=d.name,
            scheduled_for=d.expires_at,
            kind="document",
        )
        for d in upcoming_docs
    ]
    # Append expiring licenses too.
    upcoming_lics = (
        await session.execute(
            select(AgencyLicense)
            .where(
                AgencyLicense.agency_id == agency_id,
                AgencyLicense.deleted_at.is_(None),
                AgencyLicense.expires_at <= upcoming_cutoff,
                AgencyLicense.expires_at >= now,
                AgencyLicense.status.in_(
                    [LicenseStatus.WARNING, LicenseStatus.CRITICAL]
                ),
            )
            .order_by(AgencyLicense.expires_at.asc())
            .limit(5)
        )
    ).scalars().all()
    upcoming_audits.extend(
        PortalComplianceUpcomingAudit(
            id=lic.id,
            title=lic.name,
            scheduled_for=lic.expires_at,
            kind="license",
        )
        for lic in upcoming_lics
    )
    # Trim + sort by scheduled_for asc.
    upcoming_audits.sort(key=lambda a: a.scheduled_for)
    upcoming_audits = upcoming_audits[:5]

    # ----- Recent activity: most recent 5 issues by created_at -----
    recent_rows = (
        await session.execute(
            select(ComplianceIssue)
            .where(
                ComplianceIssue.agency_id == agency_id,
                ComplianceIssue.deleted_at.is_(None),
            )
            .order_by(ComplianceIssue.created_at.desc())
            .limit(5)
        )
    ).scalars().all()
    recent_activity = [
        PortalComplianceRecentActivity(
            id=row.id,
            actor_label=row.title,
            description=(
                f"Issue {row.status.value.lower()} · "
                f"severity {row.severity.value.lower()}"
            ),
            occurred_at=row.created_at,
        )
        for row in recent_rows
    ]

    return PortalComplianceResponse(
        overall_percent=overall_pct,
        sub_scores=sub_scores,
        urgent_actions=urgent_actions,
        upcoming_audits=upcoming_audits,
        recent_activity=recent_activity,
        generated_at=now,
    )


__all__ = [
    "get_portal_compliance",
    "list_my_visits",
    "load_visit_with_relations",
]