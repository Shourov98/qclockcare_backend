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

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from src.shared.domain.enums import AgencyStatus, AgencySubscriptionPlan, ProgramType
from src.shared.schemas.pagination import PaginatedResponse


# --------------------------------------------------------------------------
# Admin invite (atomic agency creation)
# --------------------------------------------------------------------------
class AgencyAdminInviteRequest(BaseModel):
    """Optional admin payload to bind an `AGENCY_ADMIN` to a new or
    existing agency in the same transaction.

    Three branches:
      * `existing_user_id` set   — promote an existing user; no
        password reset, no email.
      * `password` provided     — create the user ACTIVE and login-ready.
      * Neither                 — create the user INVITED; an
        invitation email is scheduled and a plaintext token is returned
        so the SPA can deep-link the recipient into
        `/accept-invitation?token=…`.

    Email is CITEXT-unique across the `users` table; colliding on an
    existing email surfaces as `409 DUPLICATE_RESOURCE` and the whole
    agency creation rolls back.
    """

    email: EmailStr | None = Field(default=None)
    full_name: str | None = Field(default=None, min_length=1, max_length=255)
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
            "with `email` / `password` (they're for the new-user branch)."
        ),
    )

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

    `name` is required; everything else defaults to the seed values.

    `admin` is optional. When supplied, the agency is created together
    with an `AGENCY_ADMIN` user bound to it in a single transaction —
    no orphan-agency state is possible. When omitted, the agency is
    created without an admin (legacy behaviour; use
    `POST /agencies/{id}/admins` to attach one later).
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
    admin: AgencyAdminInviteRequest | None = Field(
        default=None,
        description=(
            "Optional AGENCY_ADMIN to bind to this agency in the same "
            "transaction. See `AgencyAdminInviteRequest` for the three "
            "branches (new user / invited / existing user)."
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
