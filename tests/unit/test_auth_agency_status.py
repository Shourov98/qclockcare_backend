"""Unit tests for agency subscription/status auth enforcement."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from src.core.exceptions import AgencySuspendedError
from src.modules.agencies.models import Agency
from src.modules.appointments import models as _appt_models  # noqa: F401
from src.modules.identity import models as _identity_models  # noqa: F401
from src.modules.patients import models as _patient_models  # noqa: F401
from src.modules.staff import models as _staff_models  # noqa: F401
from src.modules.visits import models as _visits_models  # noqa: F401
from src.shared.domain.enums import AgencyStatus, AgencySubscriptionPlan


def _agency(status: AgencyStatus, **overrides: object) -> Agency:
    defaults = {
        "id": uuid.uuid4(),
        "name": "Acme Home Care",
        "status": status,
        "timezone": "America/Chicago",
        "subscription_plan": AgencySubscriptionPlan.BASIC,
        "subscription_price_cents": 2900,
        "subscription_billing_cycle": "MONTHLY",
        "trial_started_at": None,
        "trial_ends_at": None,
        "settings": {},
        "created_at": datetime(2026, 6, 1, tzinfo=UTC),
        "updated_at": datetime(2026, 6, 1, tzinfo=UTC),
        "deleted_at": None,
    }
    defaults.update(overrides)
    return Agency(**defaults)


@pytest.mark.asyncio
async def test_super_admin_without_agency_bypasses_agency_status_lookup() -> None:
    from src.modules.identity.auth_service import assert_agency_allows_auth

    session = AsyncMock()
    await assert_agency_allows_auth(session, agency_id=None)
    session.get.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [AgencyStatus.ACTIVE, AgencyStatus.TRIAL])
async def test_active_or_trial_agency_allows_auth(status: AgencyStatus) -> None:
    from src.modules.identity.auth_service import assert_agency_allows_auth

    agency_id = uuid.uuid4()
    session = AsyncMock()
    session.get.return_value = _agency(status, id=agency_id)

    await assert_agency_allows_auth(session, agency_id=agency_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [AgencyStatus.SUSPENDED, AgencyStatus.CHURNED])
async def test_suspended_or_churned_agency_rejects_auth(status: AgencyStatus) -> None:
    from src.modules.identity.auth_service import assert_agency_allows_auth

    agency_id = uuid.uuid4()
    session = AsyncMock()
    session.get.return_value = _agency(status, id=agency_id)

    with pytest.raises(AgencySuspendedError) as exc:
        await assert_agency_allows_auth(session, agency_id=agency_id)

    assert exc.value.details == {"agency_id": str(agency_id), "status": status.value}


@pytest.mark.asyncio
async def test_deleted_agency_rejects_auth() -> None:
    from src.modules.identity.auth_service import assert_agency_allows_auth

    agency_id = uuid.uuid4()
    session = AsyncMock()
    session.get.return_value = _agency(
        AgencyStatus.ACTIVE,
        id=agency_id,
        deleted_at=datetime(2026, 7, 1, tzinfo=UTC),
    )

    with pytest.raises(AgencySuspendedError) as exc:
        await assert_agency_allows_auth(session, agency_id=agency_id)

    assert exc.value.details == {"agency_id": str(agency_id), "status": "DELETED"}


@pytest.mark.asyncio
async def test_expired_trial_agency_rejects_auth() -> None:
    from src.modules.identity.auth_service import assert_agency_allows_auth

    agency_id = uuid.uuid4()
    session = AsyncMock()
    session.get.return_value = _agency(
        AgencyStatus.TRIAL,
        id=agency_id,
        trial_ends_at=datetime.now(UTC) - timedelta(days=1),
    )

    with pytest.raises(AgencySuspendedError) as exc:
        await assert_agency_allows_auth(session, agency_id=agency_id)

    assert exc.value.details == {"agency_id": str(agency_id), "status": "TRIAL_EXPIRED"}
