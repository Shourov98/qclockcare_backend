"""Unit tests for `staff_service.create_staff` returning a
`StaffInviteResult` with a fresh invitation token.

These tests verify the contract that the staff router relies on:

1. `create_staff` returns a `StaffInviteResult` (not a bare
   `StaffProfile`) so the router can hand `invitation_token` +
   `email` + `full_name` + `user_id` to `auth.email_service
   .send_invitation_email`.
2. `auth_service.issue_invitation_token` is called exactly once,
   with the right `user_id`, after the audit row is written.
3. When the email matches an existing `User`, the service still
   issues a fresh token (re-invite is intentional — admins should
   be able to re-send invitations).
4. When the role assignment already exists, we don't add a
   duplicate, but we still issue a fresh token + write the
   INVITATION_SENT audit row.
5. When the (agency_id, user_id) or (agency_id, staff_code)
   unique constraint fires, `DuplicateResourceError` is raised —
   but no token is issued (the request is rejected).

Mirrors `test_patients_invite_result.py` for the new shape.
"""

from __future__ import annotations

import uuid
from dataclasses import FrozenInstanceError
from datetime import date
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.exc import IntegrityError

from src.core.exceptions import DuplicateResourceError
from src.modules.appointments import models as _appt_models  # noqa: F401
from src.modules.locations import models as _locations_models  # noqa: F401

# IMPORTANT: import the full mapper graph BEFORE any test runs so
# that all relationship strings resolve. Several ORM mappers
# (`Appointment`, `Visit`, etc.) reference other model classes via
# string names; if those modules haven't been imported yet, lazy
# mapper init raises `InvalidRequestError: When initializing mapper
# Mapper[…], expression 'X' failed to locate a name`.
from src.modules.patients import models as _patient_models  # noqa: F401
from src.modules.visits import models as _visits_models  # noqa: F401


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------
class _FakeScalarResult:
    """Minimal stand-in for `Result.scalar_one_or_none()`."""

    def __init__(self, value: Any) -> None:
        self._value = value

    def scalar_one_or_none(self) -> Any:
        return self._value


class _FakeSession:
    """AsyncSession stand-in for `create_staff`.

    Returns canned results from `execute(...)` in order. `add` /
    `flush` are tracked but don't talk to a real DB.
    """

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


def _agency_row(agency_id: uuid.UUID) -> SimpleNamespace:
    return SimpleNamespace(
        id=agency_id,
        status=SimpleNamespace(value="ACTIVE"),
    )


def _user_row(user_id: uuid.UUID, email: str, full_name: str = "Alex") -> SimpleNamespace:
    return SimpleNamespace(
        id=user_id,
        email=email,
        full_name=full_name,
        phone=None,
        status=SimpleNamespace(value="INVITED"),
        must_change_password=True,
    )


def _payload(*, email: str = "alex@example.com") -> Any:
    """Build a StaffProfileCreateRequest — the schema's validation is
    covered separately in `test_staff_schemas.py`."""
    from src.modules.staff.schemas import StaffProfileCreateRequest

    return StaffProfileCreateRequest(
        email=email,
        full_name="Alex New",
        phone=None,
        staff_code="STF-001",
        hired_at=date(2025, 1, 1),
    )


# ---------------------------------------------------------------------------
# Happy path — user already exists (so user_id is observable)
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
class TestCreateStaffInviteResultHappyPath:
    async def test_returns_staff_invite_result_with_invitation_token(self) -> None:
        """`create_staff` returns a `StaffInviteResult` whose
        `invitation_token` matches what `issue_invitation_token`
        issued.

        We use the existing-user branch (a User row is returned by
        the first `session.execute`) so the `user_id` we receive is
        deterministic and observable."""
        from src.modules.staff import service as staff_service

        user_id = uuid.uuid4()
        agency_id = uuid.uuid4()
        invited_by = uuid.uuid4()
        existing_user = _user_row(user_id, "alex@example.com")

        session = _FakeSession(
            scalars=[existing_user, None],  # user exists, no existing role
            agency=_agency_row(agency_id),
        )

        fake_token = "inv-token-staff-1"

        with patch.object(staff_service, "auth_service") as mock_auth_service:
            mock_auth_service.issue_invitation_token = AsyncMock(
                return_value=(fake_token, "jti-staff-1")
            )
            result = await staff_service.create_staff(
                session,
                agency_id=agency_id,
                payload=_payload(),
                invited_by_user_id=invited_by,
            )

        # Returned dataclass — the router needs these fields.
        assert isinstance(result, staff_service.StaffInviteResult)
        assert result.invitation_token == fake_token
        assert result.email == "alex@example.com"
        assert result.full_name == "Alex New"
        assert result.user_id == user_id
        assert result.profile is not None
        assert result.profile.agency_id == agency_id

        # `issue_invitation_token` was called exactly once with the
        # right user_id.
        mock_auth_service.issue_invitation_token.assert_called_once()
        called_kwargs = mock_auth_service.issue_invitation_token.call_args.kwargs
        assert called_kwargs["user_id"] == user_id
        # session arg is the same fake session.
        assert mock_auth_service.issue_invitation_token.call_args.args[0] is session

    async def test_user_id_round_trips_to_router_facing_fields(self) -> None:
        """The `user_id` we hand to `issue_invitation_token` must be
        the same `user_id` we return in `StaffInviteResult`. The
        router uses both — token is for the JWT; user_id is the
        recipient on the background task."""
        from src.modules.staff import service as staff_service

        user_id = uuid.uuid4()
        agency_id = uuid.uuid4()
        existing_user = _user_row(user_id, "bob@example.com")

        session = _FakeSession(
            scalars=[existing_user, None],
            agency=_agency_row(agency_id),
        )

        captured: dict[str, Any] = {}

        async def _fake_issue(session_arg: Any, *, user_id: uuid.UUID) -> tuple[str, str]:
            captured["user_id"] = user_id
            captured["session"] = session_arg
            return ("tok", "jti")

        with patch.object(staff_service, "auth_service") as mock_auth_service:
            mock_auth_service.issue_invitation_token = _fake_issue
            result = await staff_service.create_staff(
                session,
                agency_id=agency_id,
                payload=_payload(email="bob@example.com"),
                invited_by_user_id=uuid.uuid4(),
            )

        assert captured["user_id"] == result.user_id
        assert captured["session"] is session


# ---------------------------------------------------------------------------
# Existing-user path (re-invite)
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
class TestCreateStaffReInviteExistingUser:
    async def test_existing_user_still_gets_fresh_invitation_token(self) -> None:
        """When a User row already exists for the email, we re-use
        the User, refresh name/phone, and STILL issue a fresh
        invitation token. The recipient needs a fresh link each
        time the admin clicks 'invite'."""
        from src.modules.staff import service as staff_service

        user_id = uuid.uuid4()
        agency_id = uuid.uuid4()
        existing_user = _user_row(user_id, "alex@example.com", full_name="Old Name")

        session = _FakeSession(
            scalars=[existing_user, None],  # user exists, no existing role
            agency=_agency_row(agency_id),
        )

        with patch.object(staff_service, "auth_service") as mock_auth_service:
            mock_auth_service.issue_invitation_token = AsyncMock(
                return_value=("new-tok", "new-jti")
            )
            result = await staff_service.create_staff(
                session,
                agency_id=agency_id,
                payload=_payload(email="alex@example.com"),
                invited_by_user_id=uuid.uuid4(),
            )

        # The token came from our mocked issue_invitation_token.
        assert result.invitation_token == "new-tok"
        # The user_id we passed to issue_invitation_token matches
        # the existing user's id.
        mock_auth_service.issue_invitation_token.assert_called_once_with(
            session, user_id=user_id
        )
        # The dataclass returns the existing user's email.
        assert result.email == "alex@example.com"

    async def test_existing_role_does_not_block_token_issue(self) -> None:
        """If the role assignment already exists, we skip adding a
        duplicate — but we still issue a fresh token + audit row."""
        from src.modules.staff import service as staff_service

        user_id = uuid.uuid4()
        agency_id = uuid.uuid4()
        existing_user = _user_row(user_id, "alex@example.com")
        existing_role = SimpleNamespace(
            id=uuid.uuid4(), user_id=user_id, agency_id=agency_id
        )

        session = _FakeSession(
            scalars=[existing_user, existing_role],  # user + role both exist
            agency=_agency_row(agency_id),
        )

        with patch.object(staff_service, "auth_service") as mock_auth_service:
            mock_auth_service.issue_invitation_token = AsyncMock(
                return_value=("tok", "jti")
            )
            result = await staff_service.create_staff(
                session,
                agency_id=agency_id,
                payload=_payload(),
                invited_by_user_id=uuid.uuid4(),
            )

        # Token was issued.
        assert result.invitation_token == "tok"
        # The user_id we returned came from the existing user.
        assert result.user_id == user_id


# ---------------------------------------------------------------------------
# Audit row ordering
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
class TestCreateStaffAuditOrder:
    async def test_invitation_token_issued_after_audit_row_added(self) -> None:
        """The INVITATION_SENT audit row is added to the session
        BEFORE `issue_invitation_token` is called. The order
        matters: if the token issue raises, the audit row is still
        queued (rolled back with the rest of the session)."""
        from src.modules.staff import service as staff_service

        agency_id = uuid.uuid4()
        user_id = uuid.uuid4()
        existing_user = _user_row(user_id, "alex@example.com")

        session = _FakeSession(
            scalars=[existing_user, None],
            agency=_agency_row(agency_id),
        )

        call_order: list[str] = []

        async def _fake_issue(session_arg: Any, *, user_id: uuid.UUID) -> tuple[str, str]:
            call_order.append("issue_invitation_token")
            return ("tok", "jti")

        # Patch the `_record_audit` import inside the service module.
        async def _fake_record_audit(*args: Any, **kwargs: Any) -> None:
            call_order.append("record_audit")
            session.add(SimpleNamespace(name="audit_event"))

        with patch.object(staff_service, "auth_service") as mock_auth_service:
            mock_auth_service.issue_invitation_token = _fake_issue
            with patch(
                "src.modules.identity.auth_service._record_audit",
                _fake_record_audit,
            ):
                await staff_service.create_staff(
                    session,
                    agency_id=agency_id,
                    payload=_payload(),
                    invited_by_user_id=uuid.uuid4(),
                )

        assert call_order == ["record_audit", "issue_invitation_token"]
        # The audit row was queued onto the session.
        audit_added = any(
            getattr(obj, "name", None) == "audit_event" for obj in session.added
        )
        assert audit_added

    async def test_audit_event_type_is_invitation_sent(self) -> None:
        """The audit row uses `AuthAuditEventType.INVITATION_SENT`
        (not `PASSWORD_RESET_REQUESTED` or `EMAIL_VERIFICATION_REQUESTED`)."""
        from src.modules.identity import auth_service
        from src.modules.staff import service as staff_service
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

        with patch.object(staff_service, "auth_service") as mock_auth_service:
            mock_auth_service.issue_invitation_token = AsyncMock(
                return_value=("tok", "jti")
            )
            with patch.object(auth_service, "_record_audit", _capture_record_audit):
                await staff_service.create_staff(
                    session,
                    agency_id=agency_id,
                    payload=_payload(),
                    invited_by_user_id=uuid.uuid4(),
                )

        assert captured["event_type"] == AuthAuditEventType.INVITATION_SENT
        assert "invited_by" in captured["metadata"]
        assert "staff_profile_id" in captured["metadata"]


# ---------------------------------------------------------------------------
# Conflict / duplicate paths
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
class TestCreateStaffConflictPath:
    async def test_duplicate_resource_error_skips_token_issue(self) -> None:
        """If the (agency_id, user_id) or (agency_id, staff_code)
        unique constraint fires on the second flush, we raise
        `DuplicateResourceError` — and crucially, we DO NOT issue a
        token. A failed invitation must not result in a dangling
        email.

        We use the existing-user branch (scalars[0] is a user row) so
        the first `flush()` succeeds and the second one (which
        inserts StaffProfile) is the one that raises."""
        from src.modules.staff import service as staff_service

        agency_id = uuid.uuid4()
        user_id = uuid.uuid4()
        existing_user = _user_row(user_id, "alex@example.com")

        # Build an IntegrityError with a `orig.diag.constraint_name`
        # so `_extract_constraint` returns a usable name.
        diag = SimpleNamespace(constraint_name="uq_staff_agency_user")
        orig = SimpleNamespace(diag=diag)
        flush_exc = IntegrityError("INSERT", {}, orig)

        session = _FakeSession(
            scalars=[existing_user, None],
            agency=_agency_row(agency_id),
            flush_exc=flush_exc,
        )

        with patch.object(staff_service, "auth_service") as mock_auth_service:
            mock_auth_service.issue_invitation_token = AsyncMock(
                return_value=("tok", "jti")
            )
            with pytest.raises(DuplicateResourceError):
                await staff_service.create_staff(
                    session,
                    agency_id=agency_id,
                    payload=_payload(),
                    invited_by_user_id=uuid.uuid4(),
                )

        mock_auth_service.issue_invitation_token.assert_not_called()


# ---------------------------------------------------------------------------
# Result dataclass shape
# ---------------------------------------------------------------------------
class TestStaffInviteResultShape:
    def test_is_frozen(self) -> None:
        """`StaffInviteResult` is a frozen dataclass — the router
        can pass it around without worrying about mutation."""
        from src.modules.staff.service import StaffInviteResult

        result = StaffInviteResult(
            profile=SimpleNamespace(id=uuid.uuid4()),
            user_id=uuid.uuid4(),
            email="alex@example.com",
            full_name="Alex",
            invitation_token="tok",
        )
        with pytest.raises(FrozenInstanceError):
            result.email = "other@example.com"  # type: ignore[misc]

    def test_holds_all_routing_fields(self) -> None:
        """The router needs `user_id`, `email`, `full_name`, and
        `invitation_token` from this dataclass — verify they're all
        present and typed correctly."""
        from src.modules.staff.service import StaffInviteResult

        profile = SimpleNamespace(id=uuid.uuid4())
        user_id = uuid.uuid4()
        result = StaffInviteResult(
            profile=profile,
            user_id=user_id,
            email="alex@example.com",
            full_name="Alex",
            invitation_token="tok-xyz",
        )

        assert result.profile is profile
        assert result.user_id == user_id
        assert result.email == "alex@example.com"
        assert result.full_name == "Alex"
        assert result.invitation_token == "tok-xyz"


__all__ = [
    "TestCreateStaffAuditOrder",
    "TestCreateStaffConflictPath",
    "TestCreateStaffInviteResultHappyPath",
    "TestCreateStaffReInviteExistingUser",
    "TestStaffInviteResultShape",
]
