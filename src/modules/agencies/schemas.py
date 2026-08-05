"""Agencies module — request/response DTOs for `/agencies`.

Endpoints:
  GET    /agencies                     — list all agencies (SUPER_ADMIN, paginated)
  POST   /agencies                     — create one (SUPER_ADMIN)
  GET    /agencies/{agency_id}         — fetch one (SUPER_ADMIN)
  PATCH  /agencies/{agency_id}         — partial update (SUPER_ADMIN)
  DELETE /agencies/{agency_id}         — soft-delete (SUPER_ADMIN)
  GET    /agencies/{agency_id}/programs — list programs the agency offers (SUPER_ADMIN)

State machine: an agency moves through ACTIVE → TRIAL → SUSPENDED → CHURNED.
Soft-delete is a separate operation (sets `deleted_at`); the row stays
referencable by FK but is hidden from default reads.

`settings` is a free-form JSONB column for agency-level config that
doesn't warrant its own column (e.g. feature flags, custom branding).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator, model_validator

from src.shared.domain.enums import AgencyStatus, AgencySubscriptionPlan, ProgramType
from src.shared.schemas.pagination import PaginatedResponse


# --------------------------------------------------------------------------
# Admin invite (atomic agency creation)
# --------------------------------------------------------------------------
class AgencyAdminInviteRequest(BaseModel):
    """Admin payload to bind an `AGENCY_ADMIN` to a new or existing
    agency in the same transaction.

    Two mutually exclusive branches:

      * **Promote existing user** (`existing_user_id` set) — grant the
        AGENCY_ADMIN role to a user already in the system. No
        password reset, no email. `email` / `full_name` must be
        omitted in this branch.

      * **Create new user** (`existing_user_id` unset) — `email` and
        `full_name` are **required**. `password` is optional:
          - provided (≥12 chars, policy-compliant) → user is created
            `ACTIVE`, login-ready; no email is sent.
          - omitted → user is created `INVITED` and an invitation
            email is scheduled carrying a 6-digit OTP and a deep
            link to `/accept-invitation?email=…`.

    Email is CITEXT-unique across the `users` table; colliding on an
    existing email surfaces as `409 DUPLICATE_RESOURCE` and the whole
    agency creation rolls back.
    """

    email: EmailStr | None = Field(
        default=None,
        description=(
            "Email of the new admin. Required when `existing_user_id` "
            "is unset (new-user branch); must be omitted otherwise."
        ),
    )
    full_name: str | None = Field(
        default=None,
        min_length=1,
        max_length=255,
        description=(
            "Display name of the new admin. Required when "
            "`existing_user_id` is unset; must be omitted otherwise."
        ),
    )
    phone: str | None = None
    password: str | None = Field(
        default=None,
        min_length=12,
        max_length=128,
        description=(
            "Plaintext password for the new admin. Must match the "
            "QlockCare password policy (uppercase, lowercase, digit, "
            "symbol, 12-128 chars) if provided. When omitted, the "
            "admin is created in INVITED status."
        ),
    )
    existing_user_id: UUID | None = Field(
        default=None,
        description=(
            "If set, do not create a new user — just grant the existing "
            "user the AGENCY_ADMIN role at this agency. Incompatible "
            "with `email` / `password` / `full_name`."
        ),
    )

    @model_validator(mode="after")
    def _branch_invariants(self) -> "AgencyAdminInviteRequest":
        """Enforce the discriminator rules between the two branches."""
        promoting = self.existing_user_id is not None
        if promoting:
            # Promote-existing branch — new-user fields must be empty.
            if self.email is not None:
                raise ValueError(
                    "`email` must be omitted when `existing_user_id` is set."
                )
            if self.full_name is not None:
                raise ValueError(
                    "`full_name` must be omitted when `existing_user_id` is set."
                )
            if self.password is not None:
                raise ValueError(
                    "`password` must be omitted when `existing_user_id` is set."
                )
        else:
            # New-user branch — email + full_name are required.
            if not self.email:
                raise ValueError(
                    "`email` is required when creating a new admin."
                )
            if not self.full_name:
                raise ValueError(
                    "`full_name` is required when creating a new admin."
                )
        return self

    @field_validator("full_name")
    @classmethod
    def _strip_full_name(cls, v: str | None) -> str | None:
        if v is None:
            return None
        stripped = v.strip()
        if not stripped:
            raise ValueError("must not be empty or whitespace-only")
        return stripped


# --------------------------------------------------------------------------
# Base + Create
# --------------------------------------------------------------------------
class AgencyCreateRequest(BaseModel):
    """Body for POST /agencies.

    `name` and `admin` are required. The agency is created together
    with an `AGENCY_ADMIN` user bound to it in a single transaction —
    no orphan-agency state is possible. To attach an additional admin
    after the fact (or to remediate a pre-existing orphan from before
    this schema change), use `POST /agencies/{id}/admins`.
    """

    name: str = Field(min_length=1, max_length=255)
    timezone: str = Field(default="America/Chicago", min_length=1, max_length=64)
    subscription_plan: AgencySubscriptionPlan = Field(default=AgencySubscriptionPlan.BASIC)
    start_trial: bool = Field(
        default=False,
        description="When true, creates the agency in TRIAL status for `trial_days` days.",
    )
    trial_days: int = Field(default=14, ge=1, le=90)
    settings: dict[str, Any] = Field(default_factory=dict)
    initial_program_codes: list[str] = Field(
        default_factory=list,
        description=(
            "Optional list of ProgramType values to enable at creation "
            "(e.g. ['PCA', 'ARMHS']). Unknown codes return 422."
        ),
    )
    admin: AgencyAdminInviteRequest = Field(
        description=(
            "Required AGENCY_ADMIN to bind to this agency in the same "
            "transaction. The invariant is that every agency must have "
            "at least one `AGENCY_ADMIN`; orphaned agencies are not "
            "allowed. See `AgencyAdminInviteRequest` for the two "
            "branches (promote existing user / create new user)."
        ),
    )

    @field_validator("name", "timezone")
    @classmethod
    def _strip_non_empty(cls, v: str) -> str:
        stripped = v.strip()
        if not stripped:
            raise ValueError("must not be empty or whitespace-only")
        return stripped

    @field_validator("initial_program_codes")
    @classmethod
    def _validate_program_codes(cls, v: list[str]) -> list[str]:
        valid = {pt.value for pt in ProgramType}
        unknown = [c for c in v if c not in valid]
        if unknown:
            raise ValueError(f"unknown program codes: {unknown}. valid: {sorted(valid)}")
        # Dedupe + preserve order
        seen: set[str] = set()
        out: list[str] = []
        for c in v:
            if c not in seen:
                seen.add(c)
                out.append(c)
        return out


# --------------------------------------------------------------------------
# Update (all fields optional; only set fields are written)
# --------------------------------------------------------------------------
class AgencyUpdateRequest(BaseModel):
    """Body for PATCH /agencies/{id}. All fields optional."""

    name: str | None = Field(default=None, min_length=1, max_length=255)
    timezone: str | None = Field(default=None, min_length=1, max_length=64)
    status: AgencyStatus | None = Field(default=None)
    subscription_plan: AgencySubscriptionPlan | None = Field(default=None)
    trial_ends_at: datetime | None = Field(default=None)
    settings: dict[str, Any] | None = Field(default=None)

    @field_validator("name", "timezone")
    @classmethod
    def _strip_non_empty(cls, v: str | None) -> str | None:
        if v is None:
            return None
        stripped = v.strip()
        if not stripped:
            raise ValueError("must not be empty or whitespace-only")
        return stripped


# --------------------------------------------------------------------------
# Response
# --------------------------------------------------------------------------
class AgencyResponse(BaseModel):
    """One agency — shape returned by GET / POST / PATCH."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    status: AgencyStatus
    timezone: str
    subscription_plan: AgencySubscriptionPlan
    subscription_price_cents: int
    subscription_billing_cycle: str
    trial_started_at: datetime | None
    trial_ends_at: datetime | None
    # Stripe mirror fields — populated after the agency goes through
    # Checkout. None means "no Stripe subscription yet".
    stripe_customer_id: str | None
    stripe_subscription_id: str | None
    stripe_price_id: str | None
    current_period_start: datetime | None
    current_period_end: datetime | None
    cancel_at_period_end: bool
    subscription_synced_at: datetime | None
    settings: dict[str, Any]
    created_at: datetime
    updated_at: datetime


class AgencyListResponse(PaginatedResponse[AgencyResponse]):
    """Paginated list envelope."""

    pass


# --------------------------------------------------------------------------
# Subscription packages
# --------------------------------------------------------------------------
class AgencySubscriptionPackageResponse(BaseModel):
    """One package shown in the agency subscription/pricing UI."""

    plan: AgencySubscriptionPlan
    name: str
    description: str
    monthly_price_cents: int
    billing_cycle: str
    max_team_members: int | None
    max_active_projects: int | None
    storage_gb: int | None
    is_most_popular: bool
    included_features: list[str]


class AgencySubscriptionPackageListResponse(BaseModel):
    """Available agency subscription packages."""

    data: list[AgencySubscriptionPackageResponse]


# --------------------------------------------------------------------------
# Programs
# --------------------------------------------------------------------------
class AgencyProgramResponse(BaseModel):
    """One (agency_id, program_id, is_enabled) triple with program details."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    program_id: UUID
    program_code: str
    program_name: str
    is_enabled: bool
    created_at: datetime


class AgencyProgramListResponse(BaseModel):
    """List of programs an agency offers (not paginated — bounded by `programs`)."""

    data: list[AgencyProgramResponse]


__all__ = [
    "AgencyCreateRequest",
    "AgencyListResponse",
    "AgencyProgramListResponse",
    "AgencyProgramResponse",
    "AgencyResponse",
    "AgencySubscriptionPackageListResponse",
    "AgencySubscriptionPackageResponse",
    "AgencyUpdateRequest",
]
