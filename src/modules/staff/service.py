"""Staff service — business logic for staff profiles, qualifications, availability.

Routes delegate to this module; this module is the only place that knows
how to compose ORM operations, enforce business rules, and raise the right
domain exceptions (see `src/core/exceptions.py`).

RLS is the source of truth for tenant scoping — these functions still
take an `agency_id` parameter for defence in depth and to make logging
/ audit messages explicit.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from src.core.config import settings
from src.core.exceptions import (
    ConflictError,
    DuplicateResourceError,
    InvalidStateTransitionError,
    NotFoundError,
    ValidationError,
)
from src.modules.agencies.models import Agency
from src.modules.identity.models import User, UserRoleAssignment
from src.modules.staff.models import (
    StaffAvailability,
    StaffProfile,
    StaffQualification,
)
from src.modules.staff.schemas import (
    StaffAvailabilityCreateRequest,
    StaffAvailabilityUpdateRequest,
    StaffProfileCreateRequest,
    StaffProfileUpdateRequest,
    StaffQualificationCreateRequest,
    StaffQualificationUpdateRequest,
)
from src.shared.domain.enums import UserRole, UserStatus
from src.shared.storage import get_storage


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
async def _get_staff_or_404(
    session: AsyncSession,
    *,
    staff_id: uuid.UUID,
    agency_id: uuid.UUID,
) -> StaffProfile:
    """Fetch a staff profile scoped to the caller's agency.

    Raises:
        NotFoundError: if the row doesn't exist or belongs to another agency.
    """
    stmt = (
        select(StaffProfile)
        .where(StaffProfile.id == staff_id, StaffProfile.agency_id == agency_id)
    )
    staff = (await session.execute(stmt)).scalar_one_or_none()
    if staff is None:
        raise NotFoundError(
            details={"resource": "staff_profile", "id": str(staff_id)}
        )
    return staff


async def _get_qualification_or_404(
    session: AsyncSession,
    *,
    qualification_id: uuid.UUID,
    staff_id: uuid.UUID,
    agency_id: uuid.UUID,
) -> StaffQualification:
    """Fetch a qualification scoped to staff + agency (defence in depth)."""
    stmt = select(StaffQualification).where(
        StaffQualification.id == qualification_id,
        StaffQualification.staff_id == staff_id,
        StaffQualification.agency_id == agency_id,
    )
    qual = (await session.execute(stmt)).scalar_one_or_none()
    if qual is None:
        raise NotFoundError(
            details={"resource": "staff_qualification", "id": str(qualification_id)}
        )
    return qual


async def get_qualification(
    session: AsyncSession,
    *,
    qualification_id: uuid.UUID,
    staff_id: uuid.UUID,
    agency_id: uuid.UUID,
) -> StaffQualification:
    """Public wrapper around `_get_qualification_or_404` so router
    code can fetch a single qualification without reaching into a
    private helper."""
    return await _get_qualification_or_404(
        session,
        qualification_id=qualification_id,
        staff_id=staff_id,
        agency_id=agency_id,
    )


async def _get_availability_or_404(
    session: AsyncSession,
    *,
    availability_id: uuid.UUID,
    staff_id: uuid.UUID,
    agency_id: uuid.UUID,
) -> StaffAvailability:
    stmt = select(StaffAvailability).where(
        StaffAvailability.id == availability_id,
        StaffAvailability.staff_id == staff_id,
        StaffAvailability.agency_id == agency_id,
    )
    avail = (await session.execute(stmt)).scalar_one_or_none()
    if avail is None:
        raise NotFoundError(
            details={"resource": "staff_availability", "id": str(availability_id)}
        )
    return avail


async def _assert_agency_active(session: AsyncSession, agency_id: uuid.UUID) -> None:
    """Cheap sanity check — don't create staff against a churned agency."""
    agency = await session.get(Agency, agency_id)
    if agency is None:
        raise NotFoundError(details={"resource": "agency", "id": str(agency_id)})
    if agency.status.value == "CHURNED":
        raise ConflictError(
            "Cannot modify staff on a churned agency.",
            details={"agency_id": str(agency_id), "status": agency.status.value},
        )


# --------------------------------------------------------------------------
# Staff profiles — CRUD
# --------------------------------------------------------------------------
async def create_staff(
    session: AsyncSession,
    *,
    agency_id: uuid.UUID,
    payload: StaffProfileCreateRequest,
    invited_by_user_id: uuid.UUID,
) -> StaffProfile:
    """Create a new staff member at the caller's agency.

    Creates three rows in a single transaction:
    1. `User` (status=INVITED) — auth identity
    2. `UserRoleAssignment` (role=STAFF, agency_id=…) — authorises the user
    3. `StaffProfile` (agency_id, user_id, staff_code, …) — the staff record

    The invitation email is sent out-of-band (separate module); this
    function returns the freshly-minted `StaffProfile` so the router can
    respond with the new resource.
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
            must_change_password=True,
        )
        session.add(user)
        await session.flush()  # populate user.id for the FK
    else:
        # Reuse the existing user; just refresh their name/phone. The
        # service-layer (and DB unique constraint) ensures they don't
        # already hold a profile at this agency.
        if payload.full_name:
            user.full_name = payload.full_name
        if payload.phone is not None:
            user.phone = payload.phone

    # ---- 2. Authorisation: STAFF role at this agency ----
    existing_role = (
        await session.execute(
            select(UserRoleAssignment).where(
                UserRoleAssignment.user_id == user.id,
                UserRoleAssignment.agency_id == agency_id,
                UserRoleAssignment.role == UserRole.STAFF,
            )
        )
    ).scalar_one_or_none()
    if existing_role is None:
        session.add(
            UserRoleAssignment(
                user_id=user.id,
                agency_id=agency_id,
                role=UserRole.STAFF,
            )
        )

    # ---- 3. The staff profile itself ----
    profile = StaffProfile(
        agency_id=agency_id,
        user_id=user.id,
        staff_code=payload.staff_code,
        status=UserStatus.INVITED,
        hired_at=payload.hired_at,
    )
    session.add(profile)

    try:
        await session.flush()
    except IntegrityError as exc:
        # The (agency_id, user_id) or (agency_id, staff_code) unique
        # constraint fired. Translate to a domain error.
        await session.rollback()
        raise DuplicateResourceError(
            "A staff member with this email or staff code already exists at the agency.",
            details={"constraint": _extract_constraint(exc)},
        ) from exc

    # Audit (best-effort — don't fail the whole op if audit insert fails).
    from src.modules.identity.auth_service import _record_audit
    from src.shared.domain.enums import AuthAuditEventType

    await _record_audit(
        session,
        user_id=user.id,
        event_type=AuthAuditEventType.INVITATION_SENT,
        metadata={
            "agency_id": str(agency_id),
            "invited_by": str(invited_by_user_id),
            "staff_profile_id": str(profile.id),
        },
    )

    return profile


async def get_staff(
    session: AsyncSession,
    *,
    staff_id: uuid.UUID,
    agency_id: uuid.UUID,
    with_details: bool = False,
) -> StaffProfile:
    """Fetch a single staff profile.

    `with_details=True` eagerly loads qualifications + availability.
    """
    stmt = select(StaffProfile).where(
        StaffProfile.id == staff_id, StaffProfile.agency_id == agency_id
    )
    if with_details:
        stmt = stmt.options(
            selectinload(StaffProfile.qualifications),
            selectinload(StaffProfile.availability),
        )
    staff = (await session.execute(stmt)).scalar_one_or_none()
    if staff is None:
        raise NotFoundError(
            details={"resource": "staff_profile", "id": str(staff_id)}
        )
    return staff


async def list_staff(
    session: AsyncSession,
    *,
    agency_id: uuid.UUID,
    status_filter: UserStatus | None = None,
    page: int = 1,
    page_size: int = 20,
) -> tuple[Sequence[StaffProfile], int]:
    """Paginated list of staff profiles at the caller's agency."""
    page = max(1, page)
    page_size = max(1, min(100, page_size))

    base = select(StaffProfile).where(StaffProfile.agency_id == agency_id)
    count_base = (
        select(func.count())
        .select_from(StaffProfile)
        .where(StaffProfile.agency_id == agency_id)
    )
    if status_filter is not None:
        base = base.where(StaffProfile.status == status_filter)
        count_base = count_base.where(StaffProfile.status == status_filter)

    base = (
        base.order_by(StaffProfile.created_at.desc(), StaffProfile.id)
        .limit(page_size)
        .offset((page - 1) * page_size)
    )

    rows = (await session.execute(base)).scalars().all()
    total = (await session.execute(count_base)).scalar_one()
    return rows, int(total)


async def update_staff(
    session: AsyncSession,
    *,
    staff_id: uuid.UUID,
    agency_id: uuid.UUID,
    payload: StaffProfileUpdateRequest,
) -> StaffProfile:
    """Patch a staff profile. Omitted fields are unchanged."""
    staff = await _get_staff_or_404(session, staff_id=staff_id, agency_id=agency_id)

    # Status transitions: enforce minimal business rules. Richer
    # transition graph can be added later.
    if payload.status is not None and payload.status != staff.status:
        if staff.status == UserStatus.ARCHIVED and payload.status != UserStatus.ARCHIVED:
            raise InvalidStateTransitionError(
                "Cannot transition an archived staff back to an active status.",
                details={
                    "from": staff.status.value,
                    "to": payload.status.value,
                },
            )

    # Apply the patch
    if payload.full_name is not None:
        staff.user.full_name = payload.full_name
    if payload.phone is not None:
        staff.user.phone = payload.phone
    if payload.staff_code is not None:
        staff.staff_code = payload.staff_code
    if payload.hired_at is not None:
        staff.hired_at = payload.hired_at
    if payload.terminated_at is not None:
        staff.terminated_at = payload.terminated_at
    if payload.status is not None:
        staff.status = payload.status

    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        raise DuplicateResourceError(
            "Staff code already in use at this agency.",
            details={"constraint": _extract_constraint(exc)},
        ) from exc

    return staff


async def archive_staff(
    session: AsyncSession,
    *,
    staff_id: uuid.UUID,
    agency_id: uuid.UUID,
) -> StaffProfile:
    """Soft-archive a staff member. Idempotent."""
    staff = await _get_staff_or_404(session, staff_id=staff_id, agency_id=agency_id)
    if staff.status != UserStatus.ARCHIVED:
        staff.status = UserStatus.ARCHIVED
        if staff.terminated_at is None:
            from src.shared.utils.datetime_utils import utc_now

            staff.terminated_at = utc_now().date()
        await session.flush()
    return staff


# --------------------------------------------------------------------------
# Qualifications
# --------------------------------------------------------------------------
async def list_qualifications(
    session: AsyncSession,
    *,
    staff_id: uuid.UUID,
    agency_id: uuid.UUID,
) -> Sequence[StaffQualification]:
    """List all qualifications for a staff member, newest first."""
    # Ensure the staff exists + is in the caller's agency (raises 404 if not).
    await _get_staff_or_404(session, staff_id=staff_id, agency_id=agency_id)
    stmt = (
        select(StaffQualification)
        .where(
            StaffQualification.staff_id == staff_id,
            StaffQualification.agency_id == agency_id,
        )
        .order_by(StaffQualification.created_at.desc())
    )
    return (await session.execute(stmt)).scalars().all()


async def add_qualification(
    session: AsyncSession,
    *,
    staff_id: uuid.UUID,
    agency_id: uuid.UUID,
    payload: StaffQualificationCreateRequest,
) -> StaffQualification:
    staff = await _get_staff_or_404(session, staff_id=staff_id, agency_id=agency_id)
    qual = StaffQualification(
        staff_id=staff.id,
        agency_id=agency_id,
        qualification_type=payload.qualification_type,
        program_type=payload.program_type,
        document_storage_key=payload.document_storage_key,
        issued_at=payload.issued_at,
        expires_at=payload.expires_at,
        status=payload.status,
    )
    session.add(qual)
    await session.flush()
    return qual


async def update_qualification(
    session: AsyncSession,
    *,
    qualification_id: uuid.UUID,
    staff_id: uuid.UUID,
    agency_id: uuid.UUID,
    payload: StaffQualificationUpdateRequest,
) -> StaffQualification:
    qual = await _get_qualification_or_404(
        session,
        qualification_id=qualification_id,
        staff_id=staff_id,
        agency_id=agency_id,
    )
    if payload.document_storage_key is not None:
        qual.document_storage_key = payload.document_storage_key
    if payload.issued_at is not None:
        qual.issued_at = payload.issued_at
    if payload.expires_at is not None:
        qual.expires_at = payload.expires_at
    if payload.status is not None:
        qual.status = payload.status
    if payload.program_type is not None:
        qual.program_type = payload.program_type
    await session.flush()
    return qual


async def revoke_qualification(
    session: AsyncSession,
    *,
    qualification_id: uuid.UUID,
    staff_id: uuid.UUID,
    agency_id: uuid.UUID,
) -> None:
    """Hard-revoke a qualification (mark as REVOKED — we keep history)."""
    qual = await _get_qualification_or_404(
        session,
        qualification_id=qualification_id,
        staff_id=staff_id,
        agency_id=agency_id,
    )
    if qual.status.value == "REVOKED":
        return  # idempotent
    qual.status = qual.status.__class__.REVOKED
    await session.flush()


# --------------------------------------------------------------------------
# Availability
# --------------------------------------------------------------------------
async def list_availability(
    session: AsyncSession,
    *,
    staff_id: uuid.UUID,
    agency_id: uuid.UUID,
) -> Sequence[StaffAvailability]:
    await _get_staff_or_404(session, staff_id=staff_id, agency_id=agency_id)
    stmt = (
        select(StaffAvailability)
        .where(
            StaffAvailability.staff_id == staff_id,
            StaffAvailability.agency_id == agency_id,
        )
        .order_by(StaffAvailability.created_at.desc())
    )
    return (await session.execute(stmt)).scalars().all()


async def add_availability(
    session: AsyncSession,
    *,
    staff_id: uuid.UUID,
    agency_id: uuid.UUID,
    payload: StaffAvailabilityCreateRequest,
) -> StaffAvailability:
    staff = await _get_staff_or_404(session, staff_id=staff_id, agency_id=agency_id)
    avail = StaffAvailability(
        staff_id=staff.id,
        agency_id=agency_id,
        is_unavailable=payload.is_unavailable,
        day_of_week=payload.day_of_week,
        start_time=payload.start_time,
        end_time=payload.end_time,
        specific_date=payload.specific_date,
        specific_start=payload.specific_start,
        specific_end=payload.specific_end,
        reason=payload.reason,
    )
    session.add(avail)
    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        raise ValidationError(
            "Availability row violates a check constraint.",
            details={"constraint": _extract_constraint(exc)},
        ) from exc
    return avail


async def update_availability(
    session: AsyncSession,
    *,
    availability_id: uuid.UUID,
    staff_id: uuid.UUID,
    agency_id: uuid.UUID,
    payload: StaffAvailabilityUpdateRequest,
) -> StaffAvailability:
    avail = await _get_availability_or_404(
        session,
        availability_id=availability_id,
        staff_id=staff_id,
        agency_id=agency_id,
    )
    if payload.is_unavailable is not None:
        avail.is_unavailable = payload.is_unavailable
    if payload.reason is not None:
        avail.reason = payload.reason
    await session.flush()
    return avail


async def delete_availability(
    session: AsyncSession,
    *,
    availability_id: uuid.UUID,
    staff_id: uuid.UUID,
    agency_id: uuid.UUID,
) -> None:
    avail = await _get_availability_or_404(
        session,
        availability_id=availability_id,
        staff_id=staff_id,
        agency_id=agency_id,
    )
    await session.delete(avail)
    await session.flush()


# --------------------------------------------------------------------------
# Internal
# --------------------------------------------------------------------------
def _extract_constraint(exc: IntegrityError) -> str:
    """Pull a constraint name out of a Postgres IntegrityError, if possible."""
    orig = getattr(exc, "orig", None)
    diag = getattr(orig, "diag", None)
    if diag is not None and getattr(diag, "constraint_name", None):
        return diag.constraint_name
    return "unknown"


# --------------------------------------------------------------------------
# Storage (download URL signing)
# --------------------------------------------------------------------------
def _qualifications_bucket() -> str:
    """Bucket name for staff-qualification documents, resolved from
    the active storage backend's setting."""
    if settings.STORAGE_BACKEND == "supabase":
        return settings.SUPABASE_STORAGE_BUCKET_QUALIFICATIONS
    return settings.S3_BUCKET_QUALIFICATIONS


async def build_download_url(
    *,
    storage_key: str,
) -> tuple[str, datetime]:
    """Generate a short-lived signed URL for `storage_key`.

    Returns `(url, expires_at)`. The TTL is read from
    `settings.S3_PRESIGNED_URL_TTL_SECONDS` so operators have one knob
    to control signed-URL lifetime for both backends.

    Raises:
        ValidationError: if `storage_key` is empty (the qualification
            has no attached document).
    """
    if not storage_key:
        raise ValidationError(
            "Qualification has no attached document.",
            details={"reason": "document_storage_key_is_null"},
        )

    bucket = _qualifications_bucket()
    expires_in = settings.S3_PRESIGNED_URL_TTL_SECONDS
    url = get_storage().presigned_url(
        bucket=bucket,
        key=storage_key,
        expires_in=expires_in,
        method="GET",
    )
    expires_at = datetime.now(UTC) + timedelta(seconds=expires_in)
    return url, expires_at
