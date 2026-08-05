"""Agencies service — business logic for SUPER_ADMIN agency management.

Cross-agency reads (the SUPER_ADMIN list endpoint) bypass the RLS policy
that scopes by `app.current_agency_id` by using a service role connection.
For per-agency reads, we still pass `agency_id` explicitly so the service
is auditable from logs.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.config import settings
from src.core.exceptions import ConflictError, NotFoundError, ValidationError
from src.core.security import hash_password
from src.modules.agencies.models import Agency, AgencyProgram, Program
from src.modules.agencies.schemas import (
    AgencyAdminInviteRequest,
    AgencyCreateRequest,
    AgencyUpdateRequest,
)
from src.modules.identity import otp_service
from src.modules.identity.models import User, UserRoleAssignment
from src.shared.domain.enums import AgencyStatus, AgencySubscriptionPlan, ProgramType, UserRole, UserStatus

SUBSCRIPTION_PACKAGES: dict[AgencySubscriptionPlan, dict] = {
    AgencySubscriptionPlan.BASIC: {
        "plan": AgencySubscriptionPlan.BASIC,
        "name": "Basic",
        "description": "For small agencies getting started",
        "monthly_price_cents": 2900,
        "billing_cycle": "MONTHLY",
        "max_team_members": 5,
        "max_active_projects": 10,
        "storage_gb": 5,
        "is_most_popular": False,
        "included_features": [
            "Up to 5 team members",
            "10 active projects",
            "Basic reporting & analytics",
            "Email support",
            "5GB storage",
            "Mobile app access",
            "Standard integrations",
            "Task management",
        ],
    },
    AgencySubscriptionPlan.PROFESSIONAL: {
        "plan": AgencySubscriptionPlan.PROFESSIONAL,
        "name": "Professional",
        "description": "For growing agencies with advanced needs",
        "monthly_price_cents": 7900,
        "billing_cycle": "MONTHLY",
        "max_team_members": 25,
        "max_active_projects": None,
        "storage_gb": 100,
        "is_most_popular": True,
        "included_features": [
            "Everything in Basic",
            "Up to 25 team members",
            "Unlimited projects",
            "Advanced analytics & insights",
            "Priority support (24/7)",
            "100GB storage",
            "Custom workflows",
            "Advanced integrations",
            "Time tracking & invoicing",
            "Client portals",
            "White-label options",
        ],
    },
    AgencySubscriptionPlan.ENTERPRISE: {
        "plan": AgencySubscriptionPlan.ENTERPRISE,
        "name": "Enterprise",
        "description": "For large organizations with complex needs",
        "monthly_price_cents": 9000,
        "billing_cycle": "MONTHLY",
        "max_team_members": None,
        "max_active_projects": None,
        "storage_gb": None,
        "is_most_popular": False,
        "included_features": [
            "Everything in Professional",
            "Unlimited team members",
            "Unlimited projects & storage",
            "Dedicated account manager",
            "Custom integrations & API",
            "Advanced security & compliance",
            "SSO & SAML authentication",
            "Custom training & onboarding",
            "SLA guarantees",
            "Multi-region deployment",
            "Priority feature requests",
        ],
    },
}


def _subscription_package(plan: AgencySubscriptionPlan) -> dict:
    return SUBSCRIPTION_PACKAGES[plan]


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
# Atomic admin-binding helpers
# --------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class AgencyAdminBindResult:
    """Outcome of binding an AGENCY_ADMIN to an agency.

    `invitation_otp` is `None` for the ACTIVE / existing-user paths.
    `email` is the recipient address (for the email subject + log line).
    """

    user_id: uuid.UUID
    email: str
    full_name: str
    status: UserStatus
    invitation_otp: str | None = None


async def _bind_agency_admin(
    session: AsyncSession,
    *,
    agency_id: uuid.UUID,
    payload: AgencyAdminInviteRequest,
) -> AgencyAdminBindResult:
    """Bind an `AGENCY_ADMIN` user to `agency_id` in the same session.

    Three branches (decided by which fields on `payload` are set):

      1. `existing_user_id` — promote an existing user to
         AGENCY_ADMIN at this agency. No password reset, no email.
      2. `password` provided — create a new user with
         `status='ACTIVE'`, hashed password, `email_verified_at=now()`.
         Login-ready; no email.
      3. Neither — create a new user with `status='INVITED'`,
         `password_hash=NULL`. Issue an invitation token and return its
         plaintext so the caller can schedule the invitation email.

    Raises `ConflictError` when:
      * the email already exists on a different user (new-user path)
      * the user already holds AGENCY_ADMIN at this agency
      * the unique constraint on `user_roles(user_id, role, agency_id)`
        trips for any other reason
    """
    # ---- Branch 1: promote an existing user ----
    if payload.existing_user_id is not None:
        user = (
            await session.execute(
                select(User).where(User.id == payload.existing_user_id)
            )
        ).scalar_one_or_none()
        if user is None:
            raise NotFoundError(
                details={"resource": "user", "id": str(payload.existing_user_id)},
            )
        # Check role doesn't already exist (avoids IntegrityError).
        existing_role = (
            await session.execute(
                select(UserRoleAssignment).where(
                    UserRoleAssignment.user_id == user.id,
                    UserRoleAssignment.agency_id == agency_id,
                    UserRoleAssignment.role == UserRole.AGENCY_ADMIN,
                )
            )
        ).scalar_one_or_none()
        if existing_role is not None:
            raise ConflictError(
                message="User is already an AGENCY_ADMIN at this agency.",
                details={"user_id": str(user.id), "agency_id": str(agency_id)},
            )
        session.add(
            UserRoleAssignment(
                user_id=user.id,
                agency_id=agency_id,
                role=UserRole.AGENCY_ADMIN,
            )
        )
        await session.flush()
        return AgencyAdminBindResult(
            user_id=user.id,
            email=user.email,
            full_name=user.full_name,
            status=user.status,
        )

    # ---- New-user branch (2 or 3) ----
    # `AgencyAdminInviteRequest`'s model_validator has already enforced
    # that `email` and `full_name` are set on the new-user branch (and
    # are None on the existing-user branch). We use a local narrowing
    # helper so the code below reads the non-Optional values without
    # scattering `assert` / `if` checks.
    assert payload.email is not None and payload.full_name is not None, (
        "schema validator should have rejected this"
    )
    new_user_email: str = payload.email
    new_user_full_name: str = payload.full_name

    # Check the user isn't already an AGENCY_ADMIN at this agency.
    existing_user = (
        await session.execute(select(User).where(User.email == new_user_email))
    ).scalar_one_or_none()

    if existing_user is not None and payload.password is None and existing_user.status == UserStatus.ACTIVE:
        # If we're about to INVITE an existing ACTIVE user, that's
        # almost certainly a duplicate admin request. Reject loudly.
        existing_role = (
            await session.execute(
                select(UserRoleAssignment).where(
                    UserRoleAssignment.user_id == existing_user.id,
                    UserRoleAssignment.agency_id == agency_id,
                    UserRoleAssignment.role == UserRole.AGENCY_ADMIN,
                )
            )
        ).scalar_one_or_none()
        if existing_role is not None:
            raise ConflictError(
                message="A user with this email is already an AGENCY_ADMIN at this agency.",
                details={"email": existing_user.email},
            )

    # ---- Branch 2: ACTIVE (password supplied) ----
    if payload.password is not None:
        if existing_user is not None:
            # Promote the existing user to AGENCY_ADMIN, set a fresh
            # password, mark them ACTIVE so they can log in immediately.
            existing_user.password_hash = hash_password(payload.password)
            existing_user.status = UserStatus.ACTIVE
            existing_user.full_name = new_user_full_name
            if payload.phone is not None:
                existing_user.phone = payload.phone
            user = existing_user
        else:
            user = User(
                email=new_user_email,
                full_name=new_user_full_name,
                phone=payload.phone,
                status=UserStatus.ACTIVE,
                password_hash=hash_password(payload.password),
                email_verified_at=datetime.now(tz=UTC),
                must_change_password=False,
            )
            session.add(user)
            await session.flush()
        # Check role doesn't already exist (avoids IntegrityError).
        existing_role = (
            await session.execute(
                select(UserRoleAssignment).where(
                    UserRoleAssignment.user_id == user.id,
                    UserRoleAssignment.agency_id == agency_id,
                    UserRoleAssignment.role == UserRole.AGENCY_ADMIN,
                )
            )
        ).scalar_one_or_none()
        if existing_role is not None:
            raise ConflictError(
                message="A user with this email is already an AGENCY_ADMIN at this agency.",
                details={"email": user.email},
            )
        session.add(
            UserRoleAssignment(
                user_id=user.id,
                agency_id=agency_id,
                role=UserRole.AGENCY_ADMIN,
            )
        )
        await session.flush()
        return AgencyAdminBindResult(
            user_id=user.id,
            email=user.email,
            full_name=user.full_name,
            status=user.status,
        )

    # ---- Branch 3: INVITED (no password) ----
    if existing_user is None:
        user = User(
            email=new_user_email,
            full_name=new_user_full_name,
            phone=payload.phone,
            status=UserStatus.INVITED,
            must_change_password=True,
        )
        session.add(user)
        await session.flush()
    else:
        user = existing_user
        # If the existing user already has a password hash, the
        # INVITED path doesn't make sense — they're not new. Reject
        # with a message that explains the actual state so the
        # SUPER_ADMIN can decide between re-inviting (force a fresh
        # password via Branch 2) or promoting-as-link-only (Branch 1).
        if user.password_hash is not None:
            if user.status == UserStatus.ACTIVE:
                # Already activated — no invitation email to send,
                # no new password to write. The SUPER_ADMIN's only
                # valid path forward is `existing_user_id` (promote
                # the active user to AGENCY_ADMIN at this agency).
                raise ConflictError(
                    message=(
                        f"A user with email '{user.email}' already "
                        f"exists and is ACTIVE. They cannot be "
                        f"re-invited. Use the `existing_user_id` "
                        f"branch to grant them AGENCY_ADMIN at this "
                        f"agency, or send a fresh invitation to a "
                        f"different email."
                    ),
                    details={
                        "email": user.email,
                        "user_id": str(user.id),
                        "status": user.status.value,
                    },
                )
            # INVITED / EMAIL_VERIFICATION_PENDING with a password
            # set is an unusual state, but it can happen (e.g. user
            # set a password but never finished email verification).
            # Still reject, but with a different hint.
            raise ConflictError(
                message=(
                    "A user with this email already exists and has "
                    "a password set. Provide `password` to set them "
                    "ACTIVE, or `existing_user_id` to promote them."
                ),
                details={"email": user.email},
            )
        # Update display fields.
        user.full_name = new_user_full_name
        if payload.phone is not None:
            user.phone = payload.phone
        user.status = UserStatus.INVITED

    # Check role doesn't already exist (avoids IntegrityError).
    existing_role = (
        await session.execute(
            select(UserRoleAssignment).where(
                UserRoleAssignment.user_id == user.id,
                UserRoleAssignment.agency_id == agency_id,
                UserRoleAssignment.role == UserRole.AGENCY_ADMIN,
            )
        )
    ).scalar_one_or_none()
    if existing_role is not None:
        raise ConflictError(
            message="A user with this email is already an AGENCY_ADMIN at this agency.",
            details={"email": user.email},
        )

    session.add(
        UserRoleAssignment(
            user_id=user.id,
            agency_id=agency_id,
            role=UserRole.AGENCY_ADMIN,
        )
    )
    await session.flush()

    issued = await otp_service.issue_otp(
        session,
        user=user,
    )

    return AgencyAdminBindResult(
        user_id=user.id,
        email=user.email,
        full_name=user.full_name,
        status=user.status,
        invitation_otp=issued.otp,
    )


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


def list_subscription_packages() -> list[dict]:
    """Return the supported agency subscription packages in display order."""
    return [
        dict(SUBSCRIPTION_PACKAGES[AgencySubscriptionPlan.BASIC]),
        dict(SUBSCRIPTION_PACKAGES[AgencySubscriptionPlan.PROFESSIONAL]),
        dict(SUBSCRIPTION_PACKAGES[AgencySubscriptionPlan.ENTERPRISE]),
    ]


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
) -> tuple[Agency, AgencyAdminBindResult]:
    """Insert one new agency + attach programs + bind an AGENCY_ADMIN.

    `status` starts at ACTIVE per the checklist (4.2.1).
    `initial_program_codes` is best-effort: unknown codes surface as
    422 from the schema layer before we reach here.

    `payload.admin` is **required** (see `AgencyCreateRequest`).
    The `AGENCY_ADMIN` user is bound to the new agency in the **same
    session / transaction**. If any step raises, the agency row is
    rolled back too — no orphan agencies.

    Returns `(agency, admin_bind_result)`. `admin_bind_result` is
    always populated because `payload.admin` is required.
    """
    package = _subscription_package(payload.subscription_plan)
    now = datetime.now(UTC)
    agency = Agency(
        name=payload.name,
        timezone=payload.timezone,
        status=AgencyStatus.TRIAL if payload.start_trial else AgencyStatus.ACTIVE,
        subscription_plan=payload.subscription_plan,
        subscription_price_cents=package["monthly_price_cents"],
        subscription_billing_cycle=package["billing_cycle"],
        trial_started_at=now if payload.start_trial else None,
        trial_ends_at=now + timedelta(days=payload.trial_days) if payload.start_trial else None,
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

    # `payload.admin` is required at the schema layer; the
    # `admin_bind_result` is always populated on the success path.
    admin_bind_result = await _bind_agency_admin(
        session,
        agency_id=agency.id,
        payload=payload.admin,
    )

    return agency, admin_bind_result


async def add_agency_admin(
    session: AsyncSession,
    *,
    agency_id: uuid.UUID,
    payload: AgencyAdminInviteRequest,
) -> AgencyAdminBindResult:
    """Attach an `AGENCY_ADMIN` to an existing agency (orphan-remediation).

    Same three branches as `_bind_agency_admin` (existing user, ACTIVE
    new user, INVITED new user). Used by `POST /agencies/{id}/admins`
    so SUPER_ADMIN can fix orphan agencies or add additional admins.
    """
    agency = await _get_agency_or_404(
        session, agency_id=agency_id, include_deleted=False
    )
    return await _bind_agency_admin(
        session,
        agency_id=agency.id,
        payload=payload,
    )


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
    plan = updates.get("subscription_plan")
    if plan is not None:
        package = _subscription_package(plan)
        updates["subscription_price_cents"] = package["monthly_price_cents"]
        updates["subscription_billing_cycle"] = package["billing_cycle"]

    # If the caller explicitly wants to set status to SUSPENDED or CHURNED,
    # document it via settings.suspended_at / churned_at so audit log readers
    # have a timestamp to work with.
    new_status = updates.get("status")
    now = datetime.now(UTC)
    if new_status == AgencyStatus.SUSPENDED and "settings" not in updates:
        settings = dict(agency.settings or {})
        settings.setdefault("suspended_at", now.isoformat())
        updates["settings"] = settings
    elif new_status == AgencyStatus.CHURNED and "settings" not in updates:
        settings = dict(agency.settings or {})
        settings.setdefault("churned_at", now.isoformat())
        updates["settings"] = settings
    elif new_status in {AgencyStatus.ACTIVE, AgencyStatus.TRIAL} and "settings" not in updates:
        # Clear any previously-set suspension flag so a reactivation is
        # tracked.
        settings = dict(agency.settings or {})
        if "suspended_at" in settings:
            settings["reactivated_at"] = now.isoformat()
            del settings["suspended_at"]
        if "churned_at" in settings:
            del settings["churned_at"]
        updates["settings"] = settings
    if new_status == AgencyStatus.TRIAL and agency.trial_started_at is None:
        updates.setdefault("trial_started_at", now)
    elif new_status == AgencyStatus.ACTIVE and "trial_ends_at" not in updates:
        updates["trial_ends_at"] = None

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
    "list_subscription_packages",
    "set_agency_program",
    "soft_delete_agency",
    "update_agency",
]
