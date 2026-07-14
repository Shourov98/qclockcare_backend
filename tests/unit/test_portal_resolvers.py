"""Unit tests for portal service resolvers — auth linkage checks.

Uses a tiny in-memory stand-in for AsyncSession.execute() so we don't
need a real DB. Tests the PATIENT/GUARDIAN resolver functions and the
linkage checker in isolation.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

import pytest

from src.core.exceptions import ForbiddenError
from src.modules.portal import service as portal_service
from src.shared.domain.enums import UserRole


# --------------------------------------------------------------------------
# Fake session that returns canned rows for a single SELECT call
# --------------------------------------------------------------------------
@dataclass
class _FakeResult:
    rows: list[Any] = field(default_factory=list)
    scalar: Any = None

    def scalars(self) -> _FakeResult:
        return self

    def all(self) -> list[Any]:
        return list(self.rows)

    def scalar_one_or_none(self) -> Any:
        return self.scalar


@dataclass
class _FakeSession:
    next_results: list[_FakeResult] = field(default_factory=list)
    calls: int = 0

    async def execute(self, _stmt: Any) -> _FakeResult:
        self.calls += 1
        if not self.next_results:
            return _FakeResult()
        return self.next_results.pop(0)


def _auth(role: UserRole, user_id=None, agency_id=None):
    from src.modules.identity.dependencies import AuthContext
    from src.modules.identity.schemas import CurrentUser

    return AuthContext(
        user_id=user_id or uuid.uuid4(),
        user=CurrentUser.model_construct(),
        role=role,
        agency_id=agency_id or uuid.uuid4(),
        raw_token="x",
    )


# --------------------------------------------------------------------------
# _resolve_caller_to_patients
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_patient_resolves_to_self_only() -> None:
    patient_id = uuid.uuid4()
    patient = type("P", (), {"id": patient_id})()
    session = _FakeSession(next_results=[_FakeResult(scalar=patient)])
    result = await portal_service._resolve_caller_to_patients(
        session, ctx=_auth(UserRole.PATIENT, agency_id=uuid.uuid4())
    )
    assert result == {patient_id}


@pytest.mark.asyncio
async def test_patient_without_profile_returns_empty() -> None:
    session = _FakeSession(next_results=[_FakeResult(scalar=None)])
    result = await portal_service._resolve_caller_to_patients(
        session, ctx=_auth(UserRole.PATIENT, agency_id=uuid.uuid4())
    )
    assert result == set()


@pytest.mark.asyncio
async def test_admin_role_rejected() -> None:
    with pytest.raises(ForbiddenError):
        await portal_service._resolve_caller_to_patients(
            session=_FakeSession(),
            ctx=_auth(UserRole.AGENCY_ADMIN, agency_id=uuid.uuid4()),
        )


# --------------------------------------------------------------------------
# _assert_guardian_linked_to_patient
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_guardian_with_active_legal_rel_passes() -> None:
    guardian_id = uuid.uuid4()
    guardian = type("G", (), {"id": guardian_id})()
    pid = uuid.uuid4()
    aid = uuid.uuid4()
    rel = type("R", (), {})()
    rel.is_legal = True
    rel.valid_until = None
    rel.patient_id = pid
    rel.guardian_id = guardian_id
    rel.agency_id = aid
    session = _FakeSession(next_results=[_FakeResult(rows=[rel])])
    # Should not raise.
    await portal_service._assert_guardian_linked_to_patient(
        session,
        guardian=guardian,  # type: ignore[arg-type]
        patient_id=pid,
        agency_id=aid,
    )


@pytest.mark.asyncio
async def test_guardian_with_expired_rel_rejected() -> None:
    guardian = type("G", (), {"id": uuid.uuid4()})()
    pid = uuid.uuid4()
    aid = uuid.uuid4()
    rel = type("R", (), {})()
    rel.is_legal = True
    rel.valid_until = date.today() - timedelta(days=1)
    rel.patient_id = pid
    rel.guardian_id = guardian.id
    rel.agency_id = aid
    session = _FakeSession(next_results=[_FakeResult(rows=[rel])])
    with pytest.raises(ForbiddenError) as exc:
        await portal_service._assert_guardian_linked_to_patient(
            session,
            guardian=guardian,  # type: ignore[arg-type]
            patient_id=pid,
            agency_id=aid,
        )
    assert exc.value.details.get("reason") == "relationship_expired"


@pytest.mark.asyncio
async def test_guardian_with_no_rel_rejected() -> None:
    guardian = type("G", (), {"id": uuid.uuid4()})()
    pid = uuid.uuid4()
    aid = uuid.uuid4()
    session = _FakeSession(next_results=[_FakeResult(rows=[])])
    with pytest.raises(ForbiddenError) as exc:
        await portal_service._assert_guardian_linked_to_patient(
            session,
            guardian=guardian,  # type: ignore[arg-type]
            patient_id=pid,
            agency_id=aid,
        )
    assert exc.value.details.get("reason") == "no_legal_relationship"


@pytest.mark.asyncio
async def test_guardian_with_non_legal_rel_rejected() -> None:
    """The real SQL filters out non-legal rels at the DB level.

    Simulate that by returning an empty result set.
    """
    guardian = type("G", (), {"id": uuid.uuid4()})()
    pid = uuid.uuid4()
    aid = uuid.uuid4()
    session = _FakeSession(next_results=[_FakeResult(rows=[])])
    with pytest.raises(ForbiddenError) as exc:
        await portal_service._assert_guardian_linked_to_patient(
            session,
            guardian=guardian,  # type: ignore[arg-type]
            patient_id=pid,
            agency_id=aid,
        )
    assert exc.value.details.get("reason") == "no_legal_relationship"
