"""Unit tests for `staff_service.create_staff` returning a
`StaffInviteResult` with a fresh invitation OTP.

These tests verify the contract that the staff router relies on:

1. `create_staff` returns a `StaffInviteResult` (not a bare
   `StaffProfile`) so the router can hand `invitation_otp` +
   `email` + `full_name` + `user_id` to `auth.email_service
   .send_invitation_email`.
2. `otp_service.issue_otp` is called exactly once, with the right
   `user`, after the audit row is written.
3. When the email matches an existing `User`, the service still
   issues a fresh OTP (re-invite is intentional — admins should
   be able to re-send invitations).
4. When the role assignment already exists, we don't add a
   duplicate, but we still issue a fresh OTP + write the
   INVITATION_SENT audit row.
5. When the (agency_id, user_id) or (agency_id, staff_code)
   unique constraint fires, `DuplicateResourceError` is raised —
   but no OTP is issued (the request is rejected).

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


def _issued_otp(otp: str = "482915") -> Any:
    from src.modules.identity.otp_service import OtpIssueResult

    return OtpIssueResult(
        user_id=uuid.uuid4(),
        email="alex@example.com",
        full_name="Alex",
        otp=otp,
        expires_at=None,
    )


# ---------------------------------------------------------------------------
# Happy path — user already exists (so user_id is observable)
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
class TestCreateStaffInviteResultHappyPath:
    async def test_returns_staff_invite_result_with_invitation_otp(self) -> None:
        """`create_staff` returns a `StaffInviteResult` whose
        `invitation_otp` matches what `issue_otp` issued.

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

        fake_otp = "482915"

        with patch.object(staff_service, "otp_service") as mock_otp_service:
            mock_otp_service.issue_otp = AsyncMock(
                return_value=_issued_otp(fake_otp)
            )
            result = await staff_service.create_staff(
                session,
                agency_id=agency_id,
                payload=_payload(),
                invited_by_user_id=invited_by,
            )

        # Returned dataclass — the router needs these fields.
        assert isinstance(result, staff_service.StaffInviteResult)
        assert result.invitation_otp == fake_otp
        assert result.email == "alex@example.com"
        assert result.full_name == "Alex New"
        assert result.user_id == user_id
        assert result.profile is not None
        assert result.profile.agency_id == agency_id

        # `issue_otp` was called exactly once.
        mock_otp_service.issue_otp.assert_called_once()
        call_kwargs = mock_otp_service.issue_otp.call_args.kwargs
        assert call_kwargs["user"] is existing_user

    async def test_user_id_round_trips_to_router_facing_fields(self) -> None:
        """The `user` we hand to `issue_otp` must have the same
        `id` we return in `StaffInviteResult`. The router uses both
        — the OTP is the secret; user_id is the recipient on the
        background task."""
        from src.modules.staff import service as staff_service

        user_id = uuid.uuid4()
        agency_id = uuid.uuid4()
        existing_user = _user_row(user_id, "bob@example.com")

        session = _FakeSession(
            scalars=[existing_user, None],
            agency=_agency_row(agency_id),
        )

        captured: dict[str, Any] = {}

        async def _fake_issue(session_arg: Any, *, user: Any) -> Any:
            captured["user_id"] = user.id
            captured["session"] = session_arg
            return _issued_otp()

        with patch.object(staff_service, "otp_service") as mock_otp_service:
            mock_otp_service.issue_otp = _fake_issue
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
    async def test_existing_user_still_gets_fresh_invitation_otp(self) -> None:
        """When a User row already exists for the email, we re-use
        the User, refresh name/phone, and STILL issue a fresh OTP.
        The recipient needs a fresh code each time the admin clicks
        'invite'."""
        from src.modules.staff import service as staff_service

        user_id = uuid.uuid4()
        agency_id = uuid.uuid4()
        existing_user = _user_row(user_id, "alex@example.com", full_name="Old Name")

        session = _FakeSession(
            scalars=[existing_user, None],  # user exists, no existing role
            agency=_agency_row(agency_id),
        )

        with patch.object(staff_service, "otp_service") as mock_otp_service:
            mock_otp_service.issue_otp = AsyncMock(return_value=_issued_otp("654321"))
            result = await staff_service.create_staff(
                session,
                agency_id=agency_id,
                payload=_payload(email="alex@example.com"),
                invited_by_user_id=uuid.uuid4(),
            )

        # The OTP came from our mocked issue_otp.
        assert result.invitation_otp == "654321"
        # The user we passed to issue_otp is the existing user.
        mock_otp_service.issue_otp.assert_called_once()
        assert mock_otp_service.issue_otp.call_args.kwargs["user"] is existing_user
        # The dataclass returns the existing user's email.
        assert result.email == "alex@example.com"

    async def test_existing_role_does_not_block_otp_issue(self) -> None:
        """If the role assignment already exists, we skip adding a
        duplicate — but we still issue a fresh OTP + audit row."""
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

        with patch.object(staff_service, "otp_service") as mock_otp_service:
            mock_otp_service.issue_otp = AsyncMock(return_value=_issued_otp("111111"))
            result = await staff_service.create_staff(
                session,
                agency_id=agency_id,
                payload=_payload(),
                invited_by_user_id=uuid.uuid4(),
            )

        # OTP was issued.
        assert result.invitation_otp == "111111"
        # The user_id we returned came from the existing user.
        assert result.user_id == user_id


# ---------------------------------------------------------------------------
# Audit row ordering
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
class TestCreateStaffAuditOrder:
    async def test_invitation_otp_issued_after_audit_row_added(self) -> None:
        """The INVITATION_SENT audit row is added to the session
        BEFORE `issue_otp` is called. The order matters: if the OTP
        issue raises, the audit row is still queued (rolled back
        with the rest of the session)."""
        from src.modules.staff import service as staff_service

        agency_id = uuid.uuid4()
        user_id = uuid.uuid4()
        existing_user = _user_row(user_id, "alex@example.com")

        session = _FakeSession(
            scalars=[existing_user, None],
            agency=_agency_row(agency_id),
        )

        call_order: list[str] = []

        async def _fake_issue(session_arg: Any, *, user: Any) -> Any:
            call_order.append("issue_otp")
            return _issued_otp()

        # Patch the `_record_audit` import inside the service module.
        async def _fake_record_audit(*args: Any, **kwargs: Any) -> None:
            call_order.append("record_audit")
            session.add(SimpleNamespace(name="audit_event"))

        with patch.object(staff_service, "otp_service") as mock_otp_service:
            mock_otp_service.issue_otp = _fake_issue
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

        assert call_order == ["record_audit", "issue_otp"]
        # The audit row was queued onto the session.
        audit_added = any(
            getattr(obj, "name", None) == "audit_event" for obj in session.added
        )
        assert audit_added

    async def test_audit_event_type_is_invitation_sent(self) -> None:
        """The audit row uses `AuthAuditEventType.INVITATION_SENT`
        (not `PASSWORD_RESET_REQUESTED` or `EMAIL_VERIFICATION_REQUESTED`)."""
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

        with patch.object(staff_service, "otp_service") as mock_otp_service:
            mock_otp_service.issue_otp = AsyncMock(return_value=_issued_otp())
            with patch(
                "src.modules.identity.auth_service._record_audit",
                _capture_record_audit,
            ):
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
    async def test_duplicate_resource_error_skips_otp_issue(self) -> None:
        """If the (agency_id, user_id) or (agency_id, staff_code)
        unique constraint fires on the second flush, we raise
        `DuplicateResourceError` — and crucially, we DO NOT issue
        an OTP. A failed invitation must not result in a dangling
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

        with patch.object(staff_service, "otp_service") as mock_otp_service:
            mock_otp_service.issue_otp = AsyncMock(return_value=_issued_otp())
            with pytest.raises(DuplicateResourceError):
                await staff_service.create_staff(
                    session,
                    agency_id=agency_id,
                    payload=_payload(),
                    invited_by_user_id=uuid.uuid4(),
                )

        mock_otp_service.issue_otp.assert_not_called()


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
            invitation_otp="482915",
        )
        with pytest.raises(FrozenInstanceError):
            result.email = "other@example.com"  # type: ignore[misc]

    def test_holds_all_routing_fields(self) -> None:
        """The router needs `user_id`, `email`, `full_name`, and
        `invitation_otp` from this dataclass — verify they're all
        present and typed correctly."""
        from src.modules.staff.service import StaffInviteResult

        profile = SimpleNamespace(id=uuid.uuid4())
        user_id = uuid.uuid4()
        result = StaffInviteResult(
            profile=profile,
            user_id=user_id,
            email="alex@example.com",
            full_name="Alex",
            invitation_otp="482915",
        )

        assert result.profile is profile
        assert result.user_id == user_id
        assert result.email == "alex@example.com"
        assert result.full_name == "Alex"
        assert result.invitation_otp == "482915"


__all__ = [
    "TestCreateStaffAuditOrder",
    "TestCreateStaffConflictPath",
    "TestCreateStaffInviteResultHappyPath",
    "TestCreateStaffReInviteExistingUser",
    "TestStaffInviteResultShape",
]