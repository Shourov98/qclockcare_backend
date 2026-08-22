"""Unit tests for agencies schemas — validation + ORM shape.

Covers:
  - AgencyCreateRequest: required fields (incl. `admin`), whitespace
    stripping, program code validation + dedup.
  - AgencyAdminInviteRequest: discriminator between promote-existing
    and create-new-user branches.
  - AgencyUpdateRequest: all fields optional, status enum membership.
  - AgencyResponse: ORM round-trip.
  - AgencyListResponse: pagination envelope.
  - AgencyProgramResponse / AgencyProgramListResponse: shape.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

# Import model modules so all relationship() references resolve before
# the test instantiates Agency. SQLAlchemy resolves strings lazily but the
# resolution has to happen before the first mapper is configured against
# the registry — i.e. before `Agency(**)` runs.
from src.modules.agencies import models as _agencies_models  # noqa: F401
from src.modules.agencies.models import Agency
from src.modules.agencies.schemas import (
    AgencyAdminInviteRequest,
    AgencyCreateRequest,
    AgencyListResponse,
    AgencyProgramListResponse,
    AgencyProgramResponse,
    AgencyResponse,
    AgencySubscriptionPackageListResponse,
    AgencySubscriptionPackageResponse,
    AgencyUpdateRequest,
)
from src.modules.appointments import models as _appt_models  # noqa: F401
from src.modules.identity import models as _identity_models  # noqa: F401
from src.modules.patients import models as _patient_models  # noqa: F401
from src.modules.staff import models as _staff_models  # noqa: F401
from src.modules.visits import models as _visits_models  # noqa: F401
from src.shared.domain.enums import AgencyStatus, AgencySubscriptionPlan


def _make_agency(**overrides: object) -> Agency:
    """Build an Agency ORM instance for response-shape tests."""
    defaults = {
        "id": uuid.uuid4(),
        "name": "Acme Home Care",
        "status": AgencyStatus.ACTIVE,
        "timezone": "America/Chicago",
        "subscription_plan": AgencySubscriptionPlan.BASIC,
        "subscription_price_cents": 2900,
        "subscription_billing_cycle": "MONTHLY",
        "trial_started_at": None,
        "trial_ends_at": None,
        "stripe_customer_id": None,
        "stripe_subscription_id": None,
        "stripe_price_id": None,
        "current_period_start": None,
        "current_period_end": None,
        "cancel_at_period_end": False,
        "subscription_synced_at": None,
        "settings": {"theme": "dark"},
        "created_at": datetime(2026, 6, 1, tzinfo=UTC),
        "updated_at": datetime(2026, 6, 15, tzinfo=UTC),
    }
    defaults.update(overrides)
    return Agency(**defaults)


def _invite_admin(
    *,
    email: str | None = "owner@acme.example.com",
    full_name: str | None = "Acme Owner",
    password: str | None = None,
    existing_user_id: uuid.UUID | None = None,
) -> AgencyAdminInviteRequest:
    """Build the `admin` block — required on AgencyCreateRequest.

    Defaults to the new-user INVITED branch (no password). Pass
    `existing_user_id` to use the promote-existing branch.
    """
    return AgencyAdminInviteRequest(
        email=email,
        full_name=full_name,
        password=password,
        existing_user_id=existing_user_id,
    )


# --------------------------------------------------------------------------
# AgencyCreateRequest
# --------------------------------------------------------------------------
class TestAgencyCreateRequest:
    def test_minimal_valid(self) -> None:
        r = AgencyCreateRequest(
            name="Acme Home Care",
            admin=_invite_admin(),
        )
        assert r.name == "Acme Home Care"
        assert r.timezone == "America/Chicago"
        assert r.subscription_plan == AgencySubscriptionPlan.BASIC
        assert r.start_trial is False
        assert r.trial_days == 14
        assert r.settings == {}
        assert r.initial_program_codes == []
        # admin block carried through verbatim
        assert r.admin.email == "owner@acme.example.com"
        assert r.admin.existing_user_id is None

    def test_full_valid(self) -> None:
        r = AgencyCreateRequest(
            name="Acme",
            timezone="America/New_York",
            subscription_plan=AgencySubscriptionPlan.PROFESSIONAL,
            start_trial=True,
            trial_days=30,
            settings={"branding": {"primary": "#FF0000"}},
            initial_program_codes=["PCA", "ARMHS"],
            admin=_invite_admin(),
        )
        assert r.timezone == "America/New_York"
        assert r.subscription_plan == AgencySubscriptionPlan.PROFESSIONAL
        assert r.start_trial is True
        assert r.trial_days == 30
        assert r.settings == {"branding": {"primary": "#FF0000"}}
        assert r.initial_program_codes == ["PCA", "ARMHS"]

    def test_admin_required(self) -> None:
        # Every agency must have a bound AGENCY_ADMIN; orphans are
        # not allowed at the schema layer.
        with pytest.raises(ValidationError) as exc:
            AgencyCreateRequest(name="Acme")
        assert "admin" in str(exc.value).lower()

    def test_admin_block_supports_promote_existing_branch(self) -> None:
        # When `existing_user_id` is set the new-user fields must
        # be omitted — that's how we add an orphan-remediation admin
        # without minting a new row.
        existing = uuid.uuid4()
        r = AgencyCreateRequest(
            name="Acme",
            admin=_invite_admin(
                email=None,
                full_name=None,
                password=None,
                existing_user_id=existing,
            ),
        )
        assert r.admin.existing_user_id == existing
        assert r.admin.email is None
        assert r.admin.full_name is None
        assert r.admin.password is None

    def test_blank_name_rejected(self) -> None:
        with pytest.raises(ValidationError):
            AgencyCreateRequest(name="   ", admin=_invite_admin())

    def test_blank_timezone_rejected(self) -> None:
        with pytest.raises(ValidationError):
            AgencyCreateRequest(name="Acme", timezone="   ", admin=_invite_admin())

    def test_name_too_long_rejected(self) -> None:
        with pytest.raises(ValidationError):
            AgencyCreateRequest(name="x" * 256, admin=_invite_admin())

    def test_unknown_program_code_rejected(self) -> None:
        with pytest.raises(ValidationError) as exc:
            AgencyCreateRequest(
                name="Acme",
                initial_program_codes=["PCA", "NOT_A_REAL_CODE"],
                admin=_invite_admin(),
            )
        # Error message lists the unknown codes
        assert "NOT_A_REAL_CODE" in str(exc.value)

    def test_program_codes_dedup(self) -> None:
        r = AgencyCreateRequest(
            name="Acme",
            initial_program_codes=["PCA", "ARMHS", "PCA"],
            admin=_invite_admin(),
        )
        assert r.initial_program_codes == ["PCA", "ARMHS"]

    def test_empty_program_codes_allowed(self) -> None:
        r = AgencyCreateRequest(name="Acme", initial_program_codes=[], admin=_invite_admin())
        assert r.initial_program_codes == []

    def test_trial_days_range_validated(self) -> None:
        with pytest.raises(ValidationError):
            AgencyCreateRequest(name="Acme", start_trial=True, trial_days=0, admin=_invite_admin())


# --------------------------------------------------------------------------
# AgencyAdminInviteRequest — discriminator
# --------------------------------------------------------------------------
class TestAgencyAdminInviteRequest:
    """Two mutually exclusive branches on `existing_user_id`."""

    def test_new_user_branch_email_and_full_name_required(self) -> None:
        # New-user branch — email / full_name are mandatory.
        with pytest.raises(ValidationError) as exc:
            AgencyAdminInviteRequest(email=None, full_name="Owner")
        assert "email" in str(exc.value).lower()

        with pytest.raises(ValidationError) as exc:
            AgencyAdminInviteRequest(email="owner@acme.example.com", full_name=None)
        assert "full_name" in str(exc.value).lower()

    def test_new_user_branch_whitespace_full_name_rejected(self) -> None:
        with pytest.raises(ValidationError):
            AgencyAdminInviteRequest(email="owner@acme.example.com", full_name="   ")

    def test_new_user_branch_default_password_omitted_is_invited(self) -> None:
        # No password → user is created INVITED, email carries OTP.
        r = AgencyAdminInviteRequest(email="owner@acme.example.com", full_name="Acme Owner")
        assert r.password is None
        assert r.existing_user_id is None

    def test_new_user_branch_with_policy_compliant_password_is_active(self) -> None:
        r = AgencyAdminInviteRequest(
            email="owner@acme.example.com",
            full_name="Acme Owner",
            password="LinkedAdminPass123!",
        )
        assert r.password == "LinkedAdminPass123!"

    def test_new_user_branch_short_password_rejected(self) -> None:
        with pytest.raises(ValidationError):
            AgencyAdminInviteRequest(
                email="owner@acme.example.com",
                full_name="Acme Owner",
                password="short",
            )

    def test_promote_existing_branch_email_must_be_omitted(self) -> None:
        with pytest.raises(ValidationError) as exc:
            AgencyAdminInviteRequest(
                email="owner@acme.example.com",
                existing_user_id=uuid.uuid4(),
            )
        assert "email" in str(exc.value).lower()

    def test_promote_existing_branch_full_name_must_be_omitted(self) -> None:
        with pytest.raises(ValidationError) as exc:
            AgencyAdminInviteRequest(
                full_name="Owner Name",
                existing_user_id=uuid.uuid4(),
            )
        assert "full_name" in str(exc.value).lower()

    def test_promote_existing_branch_password_must_be_omitted(self) -> None:
        with pytest.raises(ValidationError) as exc:
            AgencyAdminInviteRequest(
                password="LinkedAdminPass123!",
                existing_user_id=uuid.uuid4(),
            )
        assert "password" in str(exc.value).lower()

    def test_promote_existing_branch_minimal_valid(self) -> None:
        r = AgencyAdminInviteRequest(existing_user_id=uuid.uuid4())
        assert r.existing_user_id is not None
        assert r.email is None
        assert r.full_name is None
        assert r.password is None


# --------------------------------------------------------------------------
# AgencyUpdateRequest
# --------------------------------------------------------------------------
class TestAgencyUpdateRequest:
    def test_empty_update_valid(self) -> None:
        r = AgencyUpdateRequest()
        assert r.model_dump(exclude_unset=True) == {}

    def test_partial_update(self) -> None:
        r = AgencyUpdateRequest(name="Acme Renamed")
        dumped = r.model_dump(exclude_unset=True)
        assert dumped == {"name": "Acme Renamed"}

    def test_status_enum_validated(self) -> None:
        r = AgencyUpdateRequest(status=AgencyStatus.SUSPENDED)
        assert r.status == AgencyStatus.SUSPENDED

    def test_subscription_plan_enum_validated(self) -> None:
        r = AgencyUpdateRequest(subscription_plan=AgencySubscriptionPlan.ENTERPRISE)
        assert r.subscription_plan == AgencySubscriptionPlan.ENTERPRISE

    def test_blank_name_rejected(self) -> None:
        with pytest.raises(ValidationError):
            AgencyUpdateRequest(name="   ")


# --------------------------------------------------------------------------
# AgencyResponse + List
# --------------------------------------------------------------------------
class TestAgencyResponse:
    def test_orm_round_trip(self) -> None:
        agency = _make_agency()
        resp = AgencyResponse.model_validate(agency)
        assert resp.id == agency.id
        assert resp.name == "Acme Home Care"
        assert resp.status == AgencyStatus.ACTIVE
        assert resp.timezone == "America/Chicago"
        assert resp.subscription_plan == AgencySubscriptionPlan.BASIC
        assert resp.subscription_price_cents == 2900
        assert resp.subscription_billing_cycle == "MONTHLY"
        assert resp.trial_started_at is None
        assert resp.trial_ends_at is None
        assert resp.settings == {"theme": "dark"}
        assert resp.created_at.year == 2026

    def test_response_serializes_to_dict(self) -> None:
        agency = _make_agency()
        resp = AgencyResponse.model_validate(agency)
        d = resp.model_dump()
        assert isinstance(d["id"], uuid.UUID)
        assert d["status"] == "ACTIVE"


class TestAgencyListResponse:
    def test_envelope_shape(self) -> None:
        agencies = [_make_agency(name=f"Agency {i}") for i in range(3)]
        items = [AgencyResponse.model_validate(a) for a in agencies]
        resp = AgencyListResponse(data=items, pagination={"page": 1, "page_size": 20, "total": 3})
        assert len(resp.data) == 3
        assert resp.pagination.total == 3


class TestAgencySubscriptionPackageListResponse:
    def test_shape(self) -> None:
        package = AgencySubscriptionPackageResponse(
            plan=AgencySubscriptionPlan.PROFESSIONAL,
            name="Professional",
            description="For growing agencies with advanced needs",
            monthly_price_cents=7900,
            billing_cycle="MONTHLY",
            max_team_members=25,
            max_active_projects=None,
            storage_gb=100,
            is_most_popular=True,
            included_features=["Everything in Basic", "Up to 25 team members"],
        )
        resp = AgencySubscriptionPackageListResponse(data=[package])

        assert resp.data[0].plan == AgencySubscriptionPlan.PROFESSIONAL
        assert resp.data[0].monthly_price_cents == 7900
        assert resp.data[0].is_most_popular is True


# --------------------------------------------------------------------------
# Programs
# --------------------------------------------------------------------------
class TestAgencyProgramResponse:
    def test_shape(self) -> None:
        resp = AgencyProgramResponse(
            id=uuid.uuid4(),
            program_id=uuid.uuid4(),
            program_code="PCA",
            program_name="Personal Care Assistance",
            is_enabled=True,
            created_at=datetime(2026, 6, 1, tzinfo=UTC),
        )
        d = resp.model_dump()
        assert d["program_code"] == "PCA"
        assert d["is_enabled"] is True


class TestAgencyProgramListResponse:
    def test_empty(self) -> None:
        resp = AgencyProgramListResponse(data=[])
        assert resp.data == []
