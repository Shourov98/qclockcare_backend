"""Patients service — business logic for patient + guardian + relationship tables.

Routes delegate here. This is the only place that composes ORM operations,
enforces business rules, and raises the right domain exceptions.

RLS is the source of truth for tenant scoping; functions still take an
`agency_id` parameter for defence in depth.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from src.core.exceptions import (
    ConflictError,
    DuplicateResourceError,
    InvalidStateTransitionError,
    NotFoundError,
    ValidationError,
)
from src.modules.agencies.models import Agency
from src.modules.identity import auth_service
from src.modules.identity.models import User, UserRoleAssignment
from src.modules.patients.models import (
    GuardianProfile,
    PatientGuardianRelationship,
    PatientProfile,
)
from src.modules.patients.schemas import (
    GuardianProfileCreateRequest,
    GuardianProfileUpdateRequest,
    PatientGuardianRelationshipCreateRequest,
    PatientGuardianRelationshipUpdateRequest,
    PatientProfileCreateRequest,
    PatientProfileUpdateRequest,
)
from src.shared.domain.enums import UserRole, UserStatus


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
async def _assert_agency_active(session: AsyncSession, agency_id: uuid.UUID) -> None:
    agency = await session.get(Agency, agency_id)
    if agency is None:
        raise NotFoundError(details={"resource": "agency", "id": str(agency_id)})
    if agency.status.value == "CHURNED":
        raise ConflictError(
            "Cannot modify patients/guardians on a churned agency.",
            details={"agency_id": str(agency_id), "status": agency.status.value},
        )


async def _get_patient_or_404(
    session: AsyncSession,
    *,
    patient_id: uuid.UUID,
    agency_id: uuid.UUID,
) -> PatientProfile:
    stmt = select(PatientProfile).where(
        PatientProfile.id == patient_id, PatientProfile.agency_id == agency_id
    )
    p = (await session.execute(stmt)).scalar_one_or_none()
    if p is None:
        raise NotFoundError(
            details={"resource": "patient_profile", "id": str(patient_id)}
        )
    return p


async def _get_guardian_or_404(
    session: AsyncSession,
    *,
    guardian_id: uuid.UUID,
    agency_id: uuid.UUID,
) -> GuardianProfile:
    stmt = select(GuardianProfile).where(
        GuardianProfile.id == guardian_id,
        GuardianProfile.agency_id == agency_id,
    )
    g = (await session.execute(stmt)).scalar_one_or_none()
    if g is None:
        raise NotFoundError(
            details={"resource": "guardian_profile", "id": str(guardian_id)}
        )
    return g


async def _get_relationship_or_404(
    session: AsyncSession,
    *,
    relationship_id: uuid.UUID,
    agency_id: uuid.UUID,
) -> PatientGuardianRelationship:
    stmt = select(PatientGuardianRelationship).where(
        PatientGuardianRelationship.id == relationship_id,
        PatientGuardianRelationship.agency_id == agency_id,
    )
    r = (await session.execute(stmt)).scalar_one_or_none()
    if r is None:
        raise NotFoundError(
            details={
                "resource": "patient_guardian_relationship",
                "id": str(relationship_id),
            }
        )
    return r


def _extract_constraint(exc: IntegrityError) -> str:
    orig = getattr(exc, "orig", None)
    diag = getattr(orig, "diag", None)
    if diag is not None and getattr(diag, "constraint_name", None):
        return diag.constraint_name
    return "unknown"


# --------------------------------------------------------------------------
# Patient profiles — CRUD
# --------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class PatientInviteResult:
    """Outcome of `create_patient(...)`.

    The router schedules an invitation email via
    `auth.email_service.send_invitation_email(...)` using the
    plaintext token + recipient fields.
    """

    profile: PatientProfile
    user_id: uuid.UUID
    email: str
    full_name: str | None
    invitation_token: str


async def create_patient(
    session: AsyncSession,
    *,
    agency_id: uuid.UUID,
    payload: PatientProfileCreateRequest,
    admitted_by_user_id: uuid.UUID,
) -> PatientInviteResult:
    """Admit a new patient at the caller's agency.

    Creates three rows in a single transaction:
    1. `User` (status=INVITED) — auth identity
    2. `UserRoleAssignment` (role=PATIENT, agency_id=…) — authorises the user
    3. `PatientProfile` (agency_id, user_id, patient_code, …) — the patient record

    If a User with the same email already exists, we re-use them and only
    create the profile + role assignment. The unique constraint on
    `(agency_id, user_id)` will surface duplicates via IntegrityError.

    Issues a fresh `SingleUseToken(purpose="invitation")` and returns
    its plaintext so the caller can schedule the invitation email.
    """
    await _assert_agency_active(session, agency_id)

    # ---- 1. Look up or create the User ----
    user = (
        await session.execute(select(User).where(User.email == payload.email))
    ).scalar_one_or_none()
    if user is None:
        user = User(
            email=payload.email,
            full_name=payload.full_name,
            phone=payload.phone,
            status=UserStatus.INVITED,
        )
        session.add(user)
        await session.flush()
    else:
        if payload.full_name:
            user.full_name = payload.full_name
        if payload.phone is not None:
            user.phone = payload.phone

    # ---- 2. Authorisation: PATIENT role at this agency ----
    existing_role = (
        await session.execute(
            select(UserRoleAssignment).where(
                UserRoleAssignment.user_id == user.id,
                UserRoleAssignment.agency_id == agency_id,
                UserRoleAssignment.role == UserRole.PATIENT,
            )
        )
    ).scalar_one_or_none()
    if existing_role is None:
        session.add(
            UserRoleAssignment(
                user_id=user.id,
                agency_id=agency_id,
                role=UserRole.PATIENT,
            )
        )

    # ---- 3. The patient profile ----
    profile = PatientProfile(
        agency_id=agency_id,
        user_id=user.id,
        patient_code=payload.patient_code,
        status=UserStatus.INVITED,
        date_of_birth=payload.date_of_birth,
        gender=payload.gender,
        preferred_language=payload.preferred_language,
        admitted_at=payload.admitted_at,
    )
    session.add(profile)

    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        raise DuplicateResourceError(
            "A patient with this email or patient_code already exists at the agency.",
            details={"constraint": _extract_constraint(exc)},
        ) from exc

    from src.modules.identity.auth_service import _record_audit
    from src.shared.domain.enums import AuthAuditEventType

    await _record_audit(
        session,
        user_id=user.id,
        event_type=AuthAuditEventType.INVITATION_SENT,
        metadata={
            "agency_id": str(agency_id),
            "admitted_by": str(admitted_by_user_id),
            "patient_profile_id": str(profile.id),
        },
    )

    # Issue a fresh invitation token + return everything the router
    # needs to schedule the email.
    invitation_token, _jti = await auth_service.issue_invitation_token(
        session, user_id=user.id
    )

    return PatientInviteResult(
        profile=profile,
        user_id=user.id,
        email=user.email,
        full_name=user.full_name,
        invitation_token=invitation_token,
    )


async def get_patient(
    session: AsyncSession,
    *,
    patient_id: uuid.UUID,
    agency_id: uuid.UUID,
    with_relationships: bool = False,
) -> PatientProfile:
    stmt = select(PatientProfile).where(
        PatientProfile.id == patient_id, PatientProfile.agency_id == agency_id
    )
    if with_relationships:
        stmt = stmt.options(selectinload(PatientProfile.guardian_links))
    p = (await session.execute(stmt)).scalar_one_or_none()
    if p is None:
        raise NotFoundError(
            details={"resource": "patient_profile", "id": str(patient_id)}
        )
    return p


async def list_patients(
    session: AsyncSession,
    *,
    agency_id: uuid.UUID,
    status_filter: UserStatus | None = None,
    page: int = 1,
    page_size: int = 20,
) -> tuple[Sequence[PatientProfile], int]:
    page = max(1, page)
    page_size = max(1, min(100, page_size))

    base = select(PatientProfile).where(PatientProfile.agency_id == agency_id)
    count_base = (
        select(func.count())
        .select_from(PatientProfile)
        .where(PatientProfile.agency_id == agency_id)
    )
    if status_filter is not None:
        base = base.where(PatientProfile.status == status_filter)
        count_base = count_base.where(PatientProfile.status == status_filter)

    base = (
        base.order_by(PatientProfile.created_at.desc(), PatientProfile.id)
        .limit(page_size)
        .offset((page - 1) * page_size)
    )
    rows = (await session.execute(base)).scalars().all()
    total = (await session.execute(count_base)).scalar_one()
    return rows, int(total)


async def update_patient(
    session: AsyncSession,
    *,
    patient_id: uuid.UUID,
    agency_id: uuid.UUID,
    payload: PatientProfileUpdateRequest,
) -> PatientProfile:
    patient = await _get_patient_or_404(
        session, patient_id=patient_id, agency_id=agency_id
    )

    if payload.status is not None and payload.status != patient.status:
        if (
            patient.status == UserStatus.ARCHIVED
            and payload.status != UserStatus.ARCHIVED
        ):
            raise InvalidStateTransitionError(
                "Cannot transition an archived patient back to an active status.",
                details={"from": patient.status.value, "to": payload.status.value},
            )

    if payload.full_name is not None:
        patient.user.full_name = payload.full_name
    if payload.phone is not None:
        patient.user.phone = payload.phone
    if payload.patient_code is not None:
        patient.patient_code = payload.patient_code
    if payload.date_of_birth is not None:
        patient.date_of_birth = payload.date_of_birth
    if payload.gender is not None:
        patient.gender = payload.gender
    if payload.preferred_language is not None:
        patient.preferred_language = payload.preferred_language
    if payload.care_notes is not None:
        patient.care_notes = payload.care_notes
    if payload.admitted_at is not None:
        patient.admitted_at = payload.admitted_at
    if payload.discharged_at is not None:
        patient.discharged_at = payload.discharged_at
    if payload.status is not None:
        patient.status = payload.status

    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        raise DuplicateResourceError(
            "patient_code already in use at this agency.",
            details={"constraint": _extract_constraint(exc)},
        ) from exc
    return patient


async def archive_patient(
    session: AsyncSession,
    *,
    patient_id: uuid.UUID,
    agency_id: uuid.UUID,
) -> PatientProfile:
    patient = await _get_patient_or_404(
        session, patient_id=patient_id, agency_id=agency_id
    )
    if patient.status != UserStatus.ARCHIVED:
        patient.status = UserStatus.ARCHIVED
        if patient.discharged_at is None:
            from src.shared.utils.datetime_utils import utc_now
            patient.discharged_at = utc_now().date()
        await session.flush()
    return patient


# --------------------------------------------------------------------------
# Guardian profiles — CRUD
# --------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class GuardianInviteResult:
    """Outcome of `create_guardian(...)`.

    `new_guardian` paths in `add_patient_guardian` propagate this
    dataclass up so the router can schedule an invitation email.
    """

    profile: GuardianProfile
    user_id: uuid.UUID
    email: str
    full_name: str | None
    invitation_token: str


async def create_guardian(
    session: AsyncSession,
    *,
    agency_id: uuid.UUID,
    payload: GuardianProfileCreateRequest,
    invited_by_user_id: uuid.UUID,
) -> GuardianInviteResult:
    await _assert_agency_active(session, agency_id)

    user = (
        await session.execute(select(User).where(User.email == payload.email))
    ).scalar_one_or_none()
    if user is None:
        user = User(
            email=payload.email,
            full_name=payload.full_name,
            phone=payload.phone,
            status=UserStatus.INVITED,
        )
        session.add(user)
        await session.flush()
    else:
        if payload.full_name:
            user.full_name = payload.full_name
        if payload.phone is not None:
            user.phone = payload.phone

    existing_role = (
        await session.execute(
            select(UserRoleAssignment).where(
                UserRoleAssignment.user_id == user.id,
                UserRoleAssignment.agency_id == agency_id,
                UserRoleAssignment.role == UserRole.GUARDIAN,
            )
        )
    ).scalar_one_or_none()
    if existing_role is None:
        session.add(
            UserRoleAssignment(
                user_id=user.id,
                agency_id=agency_id,
                role=UserRole.GUARDIAN,
            )
        )

    profile = GuardianProfile(
        agency_id=agency_id,
        user_id=user.id,
        status=UserStatus.INVITED,
        contact_phone=payload.contact_phone,
        contact_email=payload.contact_email,
        notes=payload.notes,
    )
    session.add(profile)
    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        raise DuplicateResourceError(
            "A guardian with this email already exists at the agency.",
            details={"constraint": _extract_constraint(exc)},
        ) from exc

    from src.modules.identity.auth_service import _record_audit
    from src.shared.domain.enums import AuthAuditEventType

    await _record_audit(
        session,
        user_id=user.id,
        event_type=AuthAuditEventType.INVITATION_SENT,
        metadata={
            "agency_id": str(agency_id),
            "invited_by": str(invited_by_user_id),
            "guardian_profile_id": str(profile.id),
        },
    )

    # Issue a fresh invitation token + return everything the router
    # needs to schedule the email.
    invitation_token, _jti = await auth_service.issue_invitation_token(
        session, user_id=user.id
    )

    return GuardianInviteResult(
        profile=profile,
        user_id=user.id,
        email=user.email,
        full_name=user.full_name,
        invitation_token=invitation_token,
    )


async def get_guardian(
    session: AsyncSession,
    *,
    guardian_id: uuid.UUID,
    agency_id: uuid.UUID,
) -> GuardianProfile:
    return await _get_guardian_or_404(
        session, guardian_id=guardian_id, agency_id=agency_id
    )


async def list_guardians(
    session: AsyncSession,
    *,
    agency_id: uuid.UUID,
    page: int = 1,
    page_size: int = 20,
) -> tuple[Sequence[GuardianProfile], int]:
    page = max(1, page)
    page_size = max(1, min(100, page_size))
    base = (
        select(GuardianProfile)
        .where(GuardianProfile.agency_id == agency_id)
        .order_by(GuardianProfile.created_at.desc(), GuardianProfile.id)
        .limit(page_size)
        .offset((page - 1) * page_size)
    )
    count_base = (
        select(func.count())
        .select_from(GuardianProfile)
        .where(GuardianProfile.agency_id == agency_id)
    )
    rows = (await session.execute(base)).scalars().all()
    total = (await session.execute(count_base)).scalar_one()
    return rows, int(total)


async def update_guardian(
    session: AsyncSession,
    *,
    guardian_id: uuid.UUID,
    agency_id: uuid.UUID,
    payload: GuardianProfileUpdateRequest,
) -> GuardianProfile:
    guardian = await _get_guardian_or_404(
        session, guardian_id=guardian_id, agency_id=agency_id
    )
    if payload.full_name is not None:
        guardian.user.full_name = payload.full_name
    if payload.phone is not None:
        guardian.user.phone = payload.phone
    if payload.contact_phone is not None:
        guardian.contact_phone = payload.contact_phone
    if payload.contact_email is not None:
        guardian.contact_email = payload.contact_email
    if payload.notes is not None:
        guardian.notes = payload.notes
    if payload.status is not None:
        guardian.status = payload.status
    await session.flush()
    return guardian


async def archive_guardian(
    session: AsyncSession,
    *,
    guardian_id: uuid.UUID,
    agency_id: uuid.UUID,
) -> GuardianProfile:
    guardian = await _get_guardian_or_404(
        session, guardian_id=guardian_id, agency_id=agency_id
    )
    if guardian.status != UserStatus.ARCHIVED:
        guardian.status = UserStatus.ARCHIVED
        await session.flush()
    return guardian


# --------------------------------------------------------------------------
# Patient ↔ Guardian relationships
# --------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class AddPatientGuardianResult:
    """Outcome of `add_patient_guardian(...)`.

    `new_guardian` is set when the caller supplied a fresh guardian
    profile in `payload.new_guardian`. The router uses it to schedule
    an invitation email — when the caller instead supplied an
    existing `guardian_id`, no email goes out (the existing user
    already has a login path).
    """

    relationship: PatientGuardianRelationship
    new_guardian: GuardianInviteResult | None


async def list_patient_guardians(
    session: AsyncSession,
    *,
    patient_id: uuid.UUID,
    agency_id: uuid.UUID,
) -> Sequence[PatientGuardianRelationship]:
    await _get_patient_or_404(session, patient_id=patient_id, agency_id=agency_id)
    stmt = (
        select(PatientGuardianRelationship)
        .where(
            PatientGuardianRelationship.patient_id == patient_id,
            PatientGuardianRelationship.agency_id == agency_id,
        )
        .order_by(PatientGuardianRelationship.created_at.desc())
    )
    return (await session.execute(stmt)).scalars().all()


async def add_patient_guardian(
    session: AsyncSession,
    *,
    patient_id: uuid.UUID,
    agency_id: uuid.UUID,
    payload: PatientGuardianRelationshipCreateRequest,
) -> AddPatientGuardianResult:
    """Link a guardian to a patient.

    The caller supplies EITHER `guardian_id` (existing guardian) OR
    `new_guardian` (one-shot create + link). The validator on the schema
    already rejected both / neither being set.

    When `new_guardian` is supplied, the freshly-created guardian's
    `GuardianInviteResult` is propagated up so the router can schedule
    an invitation email. When `guardian_id` is supplied, no email
    goes out — the existing user already has a login path.
    """
    patient = await _get_patient_or_404(
        session, patient_id=patient_id, agency_id=agency_id
    )

    new_guardian_invite: GuardianInviteResult | None = None
    if payload.guardian_id is not None:
        guardian = await _get_guardian_or_404(
            session, guardian_id=payload.guardian_id, agency_id=agency_id
        )
    else:
        # Type narrowed by the schema validator; assert for the type checker.
        assert payload.new_guardian is not None
        new_guardian_invite = await create_guardian(
            session,
            agency_id=agency_id,
            payload=payload.new_guardian,
            invited_by_user_id=patient.user_id,  # the patient inviting their own guardian
        )
        guardian = new_guardian_invite.profile

    link = PatientGuardianRelationship(
        agency_id=agency_id,
        patient_id=patient.id,
        guardian_id=guardian.id,
        relationship_type=payload.relationship_type,
        is_legal=payload.is_legal,
        valid_from=payload.valid_from,
        valid_until=payload.valid_until,
    )
    session.add(link)
    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        raise DuplicateResourceError(
            "This patient already has this guardian with that relationship type.",
            details={"constraint": _extract_constraint(exc)},
        ) from exc

    # NOTE: We don't write a generic audit row here — the relationship
    # create is implicit. A dedicated LINK_PATIENT_GUARDIAN audit action
    # can be added in a later migration.

    return AddPatientGuardianResult(
        relationship=link,
        new_guardian=new_guardian_invite,
    )


async def update_patient_guardian(
    session: AsyncSession,
    *,
    relationship_id: uuid.UUID,
    agency_id: uuid.UUID,
    payload: PatientGuardianRelationshipUpdateRequest,
) -> PatientGuardianRelationship:
    rel = await _get_relationship_or_404(
        session, relationship_id=relationship_id, agency_id=agency_id
    )
    if payload.is_legal is not None:
        rel.is_legal = payload.is_legal
    if payload.valid_from is not None:
        rel.valid_from = payload.valid_from
    if payload.valid_until is not None:
        rel.valid_until = payload.valid_until
    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        raise ValidationError(
            "Relationship update violates a check constraint.",
            details={"constraint": _extract_constraint(exc)},
        ) from exc
    return rel


async def delete_patient_guardian(
    session: AsyncSession,
    *,
    relationship_id: uuid.UUID,
    agency_id: uuid.UUID,
) -> None:
    rel = await _get_relationship_or_404(
        session, relationship_id=relationship_id, agency_id=agency_id
    )
    await session.delete(rel)
    await session.flush()
