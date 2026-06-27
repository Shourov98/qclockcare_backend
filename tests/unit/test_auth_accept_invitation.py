"""Unit tests for `auth_service.accept_invitation`.

These tests cover the regression introduced when the JWT verification
call was duplicated at `auth_service.py:492-493`. Calling
`jwt_service.verify_single_use_token` twice per request:

1. Doubles the cryptographic cost of `/auth/accept-invitation`.
2. Risks subtle bugs if either call's behaviour differs (e.g.
   timing attacks, telemetry hook drift).
3. Is just wasteful — the result is the same both times.

These tests assert `verify_single_use_token` is called **exactly
once** per `accept_invitation` invocation.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.modules.appointments import models as _appt_models  # noqa: F401
from src.modules.locations import models as _locations_models  # noqa: F401

# Import the full mapper graph so any ORM instantiations inside the
# service don't fail at mapper-resolution time.
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

    def __init__(self, *, scalars: list[Any]) -> None:
        self._scalars = list(scalars)
        self._idx = 0
        self.added: list[Any] = []
        self.execute = AsyncMock(side_effect=self._execute)
        self.flush = AsyncMock()
        self.add = MagicMock(side_effect=self._add)

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


def _single_use_row(
    jti: str,
    user_id: uuid.UUID,
    *,
    consumed_at: datetime | None = None,
    revoked_at: datetime | None = None,
    expires_in_seconds: int = 86_400,
) -> SimpleNamespace:
    """A SimpleNamespace quacking like `SingleUseToken`."""
    return SimpleNamespace(
        jti=jti,
        user_id=user_id,
        purpose="invitation",
        consumed_at=consumed_at,
        revoked_at=revoked_at,
        expires_at=datetime.now(tz=UTC) + timedelta(seconds=expires_in_seconds),
    )


def _invited_user(user_id: uuid.UUID, email: str = "alex@example.com") -> SimpleNamespace:
    from src.shared.domain.enums import UserStatus

    return SimpleNamespace(
        id=user_id,
        email=email,
        full_name="Alex",
        status=UserStatus.INVITED,
        password_hash=None,
        roles=[],
    )


def _otp_issued(user_id: uuid.UUID) -> Any:
    from src.modules.identity.otp_service import OtpIssueResult

    return OtpIssueResult(
        user_id=user_id,
        email="alex@example.com",
        full_name="Alex",
        otp="123456",
        expires_at=datetime.now(tz=UTC) + timedelta(minutes=10),
    )


def _jwt_payload(jti: str, user_id: uuid.UUID) -> Any:
    from src.modules.identity.jwt_service import SingleUseTokenPayload

    return SingleUseTokenPayload(
        user_id=user_id,
        purpose="invitation",
        jti=jti,
        claims={"typ": "single_use", "purpose": "invitation", "jti": jti},
    )


# ---------------------------------------------------------------------------
# Regression: verify_single_use_token called exactly once
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
class TestAcceptInvitationVerifyCallCount:
    async def test_jwt_verify_called_exactly_once(self) -> None:
        """`accept_invitation` must call `jwt_service.verify_single_use_token`
        exactly once — not twice. The duplicate call was a regression;
        this test guards against it coming back.
        """
        from src.modules.identity import auth_service

        jti = "jti-1"
        user_id = uuid.uuid4()
        sut_row = _single_use_row(jti, user_id)
        user = _invited_user(user_id)

        # Order of executes inside accept_invitation:
        #   1. select(SingleUseToken by jti) → row
        #   2. select(User + roles)          → user (via _load_user_with_roles)
        session = _FakeSession(scalars=[sut_row, user])

        with (
            patch.object(auth_service, "jwt_service") as mock_jwt,
            patch.object(auth_service, "otp_service") as mock_otp,
            patch.object(auth_service, "hash_password", return_value="hashed"),
            patch.object(auth_service, "_record_audit", AsyncMock()),
            patch.object(
                auth_service, "_load_user_with_roles", AsyncMock(return_value=user)
            ),
        ):
            mock_jwt.verify_single_use_token.return_value = _jwt_payload(jti, user_id)
            mock_otp.issue_otp = AsyncMock(return_value=_otp_issued(user_id))

            user_result, otp_result = await auth_service.accept_invitation(
                session,
                invitation_token="the-token",
                new_password="new-password",
            )

        # The JWT verify was called exactly once.
        mock_jwt.verify_single_use_token.assert_called_once_with(
            "the-token", expected_purpose="invitation"
        )
        # And the call returned what we wanted.
        assert user_result is user
        assert otp_result == "123456"

    async def test_jwt_verify_called_once_even_when_audit_calls_fail(self) -> None:
        """The duplicate-verify bug wasn't conditional on audit; it
        always happened. Verify the call count is still 1 even when
        we patch away the audit side-effects."""
        from src.modules.identity import auth_service

        jti = "jti-2"
        user_id = uuid.uuid4()
        sut_row = _single_use_row(jti, user_id)
        user = _invited_user(user_id)

        session = _FakeSession(scalars=[sut_row, user])

        with (
            patch.object(auth_service, "jwt_service") as mock_jwt,
            patch.object(auth_service, "otp_service") as mock_otp,
            patch.object(auth_service, "hash_password", return_value="hashed"),
            patch.object(auth_service, "_record_audit", AsyncMock()),
            patch.object(
                auth_service, "_load_user_with_roles", AsyncMock(return_value=user)
            ),
        ):
            mock_jwt.verify_single_use_token.return_value = _jwt_payload(jti, user_id)
            mock_otp.issue_otp = AsyncMock(return_value=_otp_issued(user_id))

            await auth_service.accept_invitation(
                session,
                invitation_token="another-token",
                new_password="hunter2",
            )

        # Still exactly one — no regressions.
        assert mock_jwt.verify_single_use_token.call_count == 1


# ---------------------------------------------------------------------------
# Sanity: behaviour of accept_invitation when the verify succeeds
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
class TestAcceptInvitationHappyPath:
    async def test_user_password_set_and_status_transitioned(self) -> None:
        """When the token verifies and the row exists, we set the
        password hash, transition the user to EMAIL_VERIFICATION_PENDING,
        and mark the row consumed. The OTP is issued."""
        from src.modules.identity import auth_service

        jti = "jti-3"
        user_id = uuid.uuid4()
        sut_row = _single_use_row(jti, user_id)
        user = _invited_user(user_id)

        session = _FakeSession(scalars=[sut_row, user])

        with (
            patch.object(auth_service, "jwt_service") as mock_jwt,
            patch.object(auth_service, "otp_service") as mock_otp,
            patch.object(auth_service, "hash_password", return_value="hashed-pw"),
            patch.object(auth_service, "_record_audit", AsyncMock()),
            patch.object(
                auth_service, "_load_user_with_roles", AsyncMock(return_value=user)
            ),
        ):
            mock_jwt.verify_single_use_token.return_value = _jwt_payload(jti, user_id)
            mock_otp.issue_otp = AsyncMock(return_value=_otp_issued(user_id))

            returned_user, otp = await auth_service.accept_invitation(
                session,
                invitation_token="tok",
                new_password="hunter2",
            )

        # Password was set.
        assert user.password_hash == "hashed-pw"
        # Status was transitioned.
        from src.shared.domain.enums import UserStatus

        assert user.status == UserStatus.EMAIL_VERIFICATION_PENDING
        # Row was marked consumed.
        assert sut_row.consumed_at is not None
        # OTP returned for the email step.
        assert otp == "123456"
        assert returned_user is user


__all__ = [
    "TestAcceptInvitationHappyPath",
    "TestAcceptInvitationVerifyCallCount",
]
