"""Unit tests for `patients_service.create_patient`,
`patients_service.create_guardian`, and
`patients_service.add_patient_guardian` returning
`*InviteResult` dataclasses with fresh invitation tokens.

These tests verify the contracts the patient / guardian routers
rely on:

1. `create_patient` returns a `PatientInviteResult` whose
   `invitation_token` matches what `issue_invitation_token` issued
   (mirrors `test_staff_invite_result.py`).
2. `create_guardian` returns a `GuardianInviteResult`.
3. `add_patient_guardian` returns `AddPatientGuardianResult` with
   `new_guardian` set iff the caller supplied `new_guardian` (not
   `guardian_id`).
4. `auth_service.issue_invitation_token` is called exactly once
   per `create_*` invocation, with the right `user_id`.
5. Audit row uses `AuthAuditEventType.INVITATION_SENT`.
6. When the unique constraint fires, `DuplicateResourceError` is
   raised and no token is issued.
"""

from __future__ import annotations

import uuid
from dataclasses import FrozenInstanceError
from datetime import date
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy.exc import IntegrityError

from src.core.exceptions import DuplicateResourceError
from src.modules.appointments import models as _appt_models  # noqa: F401
from src.modules.locations import models as _locations_models  # noqa: F401

# IMPORTANT: import the full mapper graph BEFORE any test runs so
# that all relationship strings resolve (see the matching note in
# `test_staff_invite_result.py`).
from src.modules.patients import models as _patient_models  # noqa: F401
from src.modules.staff import models as _staff_models  # noqa: F401
from src.modules.visits import models as _visits_models  # noqa: F401


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------
class _FakeScalarResult:
    def __init__(self, value: Any) -> None:
        self._value = value

    def scalar_one_or_none(self) -> Any:
        return self._value


class _FakeSession:
    """AsyncSession stand-in — returns canned scalar results in order,
    tracks add/flush, no real DB I/O."""

    def __init__(
        self,
        *,
        scalars: list[Any],
        agency: Any = None,
        flush_exc: IntegrityError | None = None,
    ) -> None:
        self._scalars = list(scalars)
        self._idx = 0
        self.added: list[Any] = []
        self.get = AsyncMock(return_value=agency)
        self.execute = AsyncMock(side_effect=self._execute)
        self._flush_exc = flush_exc
        self._flushed = 0
        self.flush = AsyncMock(side_effect=self._flush)
        self.add = MagicMock(side_effect=self._add)
        self.rollback = AsyncMock()

    async def _execute(self, stmt: Any) -> _FakeScalarResult:
        if self._idx >= len(self._scalars):
            raise AssertionError(
                f"_FakeSession.execute called too many times "
                f"({self._idx + 1} > {len(self._scalars)})"
            )
        value = self._scalars[self._idx]
        self._idx += 1
        return _FakeScalarResult(value)

    def _add(self, obj: Any) -> None:
        self.added.append(obj)

    async def _flush(self) -> None:
        self._flushed += 1
        if self._flush_exc is not None:
            exc = self._flush_exc
            self._flush_exc = None
            raise exc


# Need MagicMock import for `_add` side_effect.
from unittest.mock import MagicMock  # noqa: E402


def _agency_row(agency_id: uuid.UUID) -> SimpleNamespace:
    return SimpleNamespace(
        id=agency_id, status=SimpleNamespace(value="ACTIVE")
    )


def _user_row(user_id: uuid.UUID, email: str, full_name: str = "Alex") -> SimpleNamespace:
    return SimpleNamespace(
        id=user_id,
        email=email,
        full_name=full_name,
        phone=None,
        status=SimpleNamespace(value="INVITED"),
    )


def _patient_row(patient_id: uuid.UUID, user_id: uuid.UUID, agency_id: uuid.UUID) -> SimpleNamespace:
    """A SimpleNamespace quacking like `Patient` for
    `add_patient_guardian` (`patient.user_id` is read by the service)."""
    return SimpleNamespace(
        id=patient_id,
        agency_id=agency_id,
        user_id=user_id,
    )


def _patient_payload(*, email: str = "alex@example.com") -> Any:
    from src.modules.patients.schemas import PatientProfileCreateRequest

    return PatientProfileCreateRequest(
        email=email,
        full_name="Alex Patient",
        phone=None,
        patient_code="PAT-001",
        date_of_birth=date(1990, 5, 15),
        gender="F",
        preferred_language="en",
        admitted_at=date(2025, 1, 1),
    )


def _guardian_payload(*, email: str = "guard@example.com") -> Any:
    from src.modules.patients.schemas import GuardianProfileCreateRequest

    return GuardianProfileCreateRequest(
        email=email,
        full_name="Sam Guardian",
        phone="+15551234567",
        contact_phone="+15551234567",
        contact_email=email,
        notes="Spouse",
    )


def _relationship_payload(*, guardian_id: uuid.UUID | None = None) -> Any:
    from src.modules.patients.schemas import (
        GuardianProfileCreateRequest,
        PatientGuardianRelationshipCreateRequest,
    )
    from src.shared.domain.enums import RelationshipType

    if guardian_id is not None:
        return PatientGuardianRelationshipCreateRequest(
            relationship_type=RelationshipType.SPOUSE,
            is_legal=True,
            guardian_id=guardian_id,
        )
    return PatientGuardianRelationshipCreateRequest(
        relationship_type=RelationshipType.SPOUSE,
        is_legal=True,
        new_guardian=GuardianProfileCreateRequest(
            email="newguard@example.com",
            full_name="New Guardian",
            contact_email="newguard@example.com",
        ),
    )


# ---------------------------------------------------------------------------
# create_patient
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
class TestCreatePatientInviteResult:
    async def test_returns_patient_invite_result_with_invitation_token(self) -> None:
        from src.modules.patients import service as patients_service

        user_id = uuid.uuid4()
        agency_id = uuid.uuid4()
        existing_user = _user_row(user_id, "alex@example.com")

        session = _FakeSession(
            scalars=[existing_user, None],  # user exists, no existing role
            agency=_agency_row(agency_id),
        )

        fake_token = "inv-token-patient-1"

        with patch.object(patients_service, "auth_service") as mock_auth_service:
            mock_auth_service.issue_invitation_token = AsyncMock(
                return_value=(fake_token, "jti-patient-1")
            )
            result = await patients_service.create_patient(
                session,
                agency_id=agency_id,
                payload=_patient_payload(),
                admitted_by_user_id=uuid.uuid4(),
            )

        assert isinstance(result, patients_service.PatientInviteResult)
        assert result.invitation_token == fake_token
        assert result.email == "alex@example.com"
        assert result.full_name == "Alex Patient"
        assert result.user_id == user_id
        assert result.profile is not None
        assert result.profile.agency_id == agency_id

        # Token was issued exactly once with the right user_id.
        mock_auth_service.issue_invitation_token.assert_called_once_with(
            session, user_id=user_id
        )

    async def test_audit_event_type_is_invitation_sent(self) -> None:
        from src.modules.identity import auth_service
        from src.modules.patients import service as patients_service
        from src.shared.domain.enums import AuthAuditEventType

        agency_id = uuid.uuid4()
        user_id = uuid.uuid4()
        existing_user = _user_row(user_id, "alex@example.com")

        captured: dict[str, Any] = {}

        async def _capture_record_audit(
            session_arg: Any, *, user_id: Any, event_type: Any, **kwargs: Any
        ) -> None:
            captured["event_type"] = event_type
            captured["user_id"] = user_id
            captured["metadata"] = kwargs.get("metadata")

        session = _FakeSession(
            scalars=[existing_user, None],
            agency=_agency_row(agency_id),
        )

        with patch.object(patients_service, "auth_service") as mock_auth_service:
            mock_auth_service.issue_invitation_token = AsyncMock(
                return_value=("tok", "jti")
            )
            with patch.object(
                auth_service, "_record_audit", _capture_record_audit
            ):
                await patients_service.create_patient(
                    session,
                    agency_id=agency_id,
                    payload=_patient_payload(),
                    admitted_by_user_id=uuid.uuid4(),
                )

        assert captured["event_type"] == AuthAuditEventType.INVITATION_SENT
        assert "admitted_by" in captured["metadata"]
        assert "patient_profile_id" in captured["metadata"]

    async def test_duplicate_resource_error_skips_token_issue(self) -> None:
        from src.modules.patients import service as patients_service

        agency_id = uuid.uuid4()
        user_id = uuid.uuid4()
        existing_user = _user_row(user_id, "alex@example.com")

        diag = SimpleNamespace(constraint_name="uq_patient_agency_user")
        orig = SimpleNamespace(diag=diag)
        flush_exc = IntegrityError("INSERT", {}, orig)

        session = _FakeSession(
            scalars=[existing_user, None],
            agency=_agency_row(agency_id),
            flush_exc=flush_exc,
        )

        with patch.object(patients_service, "auth_service") as mock_auth_service:
            mock_auth_service.issue_invitation_token = AsyncMock(
                return_value=("tok", "jti")
            )
            with pytest.raises(DuplicateResourceError):
                await patients_service.create_patient(
                    session,
                    agency_id=agency_id,
                    payload=_patient_payload(),
                    admitted_by_user_id=uuid.uuid4(),
                )

        mock_auth_service.issue_invitation_token.assert_not_called()


# ---------------------------------------------------------------------------
# create_guardian
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
class TestCreateGuardianInviteResult:
    async def test_returns_guardian_invite_result_with_invitation_token(self) -> None:
        from src.modules.patients import service as patients_service

        user_id = uuid.uuid4()
        agency_id = uuid.uuid4()
        existing_user = _user_row(user_id, "guard@example.com")

        session = _FakeSession(
            scalars=[existing_user, None],  # user exists, no existing role
            agency=_agency_row(agency_id),
        )

        fake_token = "inv-token-guardian-1"

        with patch.object(patients_service, "auth_service") as mock_auth_service:
            mock_auth_service.issue_invitation_token = AsyncMock(
                return_value=(fake_token, "jti-guardian-1")
            )
            result = await patients_service.create_guardian(
                session,
                agency_id=agency_id,
                payload=_guardian_payload(),
                invited_by_user_id=uuid.uuid4(),
            )

        assert isinstance(result, patients_service.GuardianInviteResult)
        assert result.invitation_token == fake_token
        assert result.email == "guard@example.com"
        assert result.full_name == "Sam Guardian"
        assert result.user_id == user_id
        assert result.profile is not None
        assert result.profile.agency_id == agency_id

        mock_auth_service.issue_invitation_token.assert_called_once_with(
            session, user_id=user_id
        )

    async def test_duplicate_resource_error_skips_token_issue(self) -> None:
        from src.modules.patients import service as patients_service

        agency_id = uuid.uuid4()
        user_id = uuid.uuid4()
        existing_user = _user_row(user_id, "guard@example.com")

        diag = SimpleNamespace(constraint_name="uq_guardian_agency_user")
        orig = SimpleNamespace(diag=diag)
        flush_exc = IntegrityError("INSERT", {}, orig)

        session = _FakeSession(
            scalars=[existing_user, None],
            agency=_agency_row(agency_id),
            flush_exc=flush_exc,
        )

        with patch.object(patients_service, "auth_service") as mock_auth_service:
            mock_auth_service.issue_invitation_token = AsyncMock(
                return_value=("tok", "jti")
            )
            with pytest.raises(DuplicateResourceError):
                await patients_service.create_guardian(
                    session,
                    agency_id=agency_id,
                    payload=_guardian_payload(),
                    invited_by_user_id=uuid.uuid4(),
                )

        mock_auth_service.issue_invitation_token.assert_not_called()


# ---------------------------------------------------------------------------
# add_patient_guardian — propagation paths
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
class TestAddPatientGuardianPropagation:
    async def test_existing_guardian_id_path_returns_no_new_guardian(self) -> None:
        """When the caller passes `guardian_id=<existing>`, no new
        guardian is created and `result.new_guardian` is None. The
        router must NOT schedule an invitation email in this case."""
        from src.modules.patients import service as patients_service

        agency_id = uuid.uuid4()
        patient_id = uuid.uuid4()
        guardian_id = uuid.uuid4()
        existing_patient = _patient_row(patient_id, user_id=uuid.uuid4(), agency_id=agency_id)
        existing_guardian = SimpleNamespace(
            id=guardian_id, agency_id=agency_id, user_id=uuid.uuid4()
        )

        # 1. select(PatientProfile by id) → existing patient
        # 2. select(GuardianProfile by id) → existing guardian
        session = _FakeSession(
            scalars=[existing_patient, existing_guardian],
            agency=_agency_row(agency_id),
        )

        with patch.object(patients_service, "auth_service") as mock_auth_service:
            mock_auth_service.issue_invitation_token = AsyncMock(
                return_value=("tok", "jti")
            )
            result = await patients_service.add_patient_guardian(
                session,
                patient_id=patient_id,
                agency_id=agency_id,
                payload=_relationship_payload(guardian_id=guardian_id),
            )

        assert isinstance(result, patients_service.AddPatientGuardianResult)
        # No new guardian → no email scheduled.
        assert result.new_guardian is None
        # Relationship is populated.
        assert result.relationship is not None
        # No token was issued (the existing guardian already has a login path).
        mock_auth_service.issue_invitation_token.assert_not_called()

    async def test_new_guardian_path_propagates_invite_result(self) -> None:
        """When the caller passes `new_guardian=…`, a fresh guardian
        is created (via the internal `create_guardian` call) and the
        resulting `GuardianInviteResult` is propagated up. The router
        uses this to schedule the invitation email."""
        from src.modules.patients import service as patients_service

        agency_id = uuid.uuid4()
        patient_id = uuid.uuid4()
        patient = _patient_row(patient_id, user_id=uuid.uuid4(), agency_id=agency_id)

        # Calls happen in this order inside add_patient_guardian →
        # create_guardian:
        #   1. select(PatientProfile by id)        → existing patient
        #   2. select(User by email)               → None (new guardian)
        #   3. select(UserRoleAssignment)          → None
        session = _FakeSession(
            scalars=[patient, None, None],
            agency=_agency_row(agency_id),
        )

        fake_token = "inv-token-new-guard-1"

        with patch.object(patients_service, "auth_service") as mock_auth_service:
            mock_auth_service.issue_invitation_token = AsyncMock(
                return_value=(fake_token, "jti-new-guard-1")
            )
            result = await patients_service.add_patient_guardian(
                session,
                patient_id=patient_id,
                agency_id=agency_id,
                payload=_relationship_payload(),
            )

        assert isinstance(result, patients_service.AddPatientGuardianResult)
        assert result.new_guardian is not None
        # The propagated dataclass is a GuardianInviteResult.
        assert isinstance(
            result.new_guardian, patients_service.GuardianInviteResult
        )
        assert result.new_guardian.invitation_token == fake_token
        assert result.new_guardian.email == "newguard@example.com"
        assert result.new_guardian.full_name == "New Guardian"
        assert result.new_guardian.profile is not None
        # Token was issued exactly once (for the new guardian).
        mock_auth_service.issue_invitation_token.assert_called_once()


# ---------------------------------------------------------------------------
# Dataclass shapes
# ---------------------------------------------------------------------------
class TestPatientInviteResultShape:
    def test_patient_invite_result_is_frozen(self) -> None:
        from src.modules.patients.service import PatientInviteResult

        result = PatientInviteResult(
            profile=SimpleNamespace(id=uuid.uuid4()),
            user_id=uuid.uuid4(),
            email="a@example.com",
            full_name="A",
            invitation_token="t",
        )
        with pytest.raises(FrozenInstanceError):
            result.email = "b@example.com"  # type: ignore[misc]

    def test_guardian_invite_result_is_frozen(self) -> None:
        from src.modules.patients.service import GuardianInviteResult

        result = GuardianInviteResult(
            profile=SimpleNamespace(id=uuid.uuid4()),
            user_id=uuid.uuid4(),
            email="g@example.com",
            full_name="G",
            invitation_token="t",
        )
        with pytest.raises(FrozenInstanceError):
            result.email = "b@example.com"  # type: ignore[misc]

    def test_add_patient_guardian_result_is_frozen(self) -> None:
        from src.modules.patients.service import AddPatientGuardianResult

        result = AddPatientGuardianResult(
            relationship=SimpleNamespace(id=uuid.uuid4()),
            new_guardian=None,
        )
        with pytest.raises(FrozenInstanceError):
            result.new_guardian = SimpleNamespace()  # type: ignore[misc]

    def test_add_patient_guardian_result_new_guardian_optional(self) -> None:
        """`new_guardian` is `Optional` — None for existing-guardian
        path, set for new_guardian path."""
        from src.modules.patients.service import AddPatientGuardianResult

        # None branch.
        r1 = AddPatientGuardianResult(
            relationship=SimpleNamespace(id=uuid.uuid4()),
            new_guardian=None,
        )
        assert r1.new_guardian is None

        # Set branch.
        from src.modules.patients.service import GuardianInviteResult

        invite = GuardianInviteResult(
            profile=SimpleNamespace(id=uuid.uuid4()),
            user_id=uuid.uuid4(),
            email="g@example.com",
            full_name="G",
            invitation_token="t",
        )
        r2 = AddPatientGuardianResult(
            relationship=SimpleNamespace(id=uuid.uuid4()),
            new_guardian=invite,
        )
        assert r2.new_guardian is invite


__all__ = [
    "TestAddPatientGuardianPropagation",
    "TestCreateGuardianInviteResult",
    "TestCreatePatientInviteResult",
    "TestPatientInviteResultShape",
]
