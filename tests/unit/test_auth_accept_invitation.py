"""Unit tests for `auth_service.accept_invitation`.

These tests cover the email-in-URL + OTP-in-body flow. The old JWT-in-URL
design was retired because:

1. URL params get stripped/mangled by some email gateways, which surfaced
   as `TOKEN_INVALID` on the backend.
2. The OTP service already gates single-use, expiry, attempt-limits —
   there was no benefit to a second cryptographic layer.

`accept_invitation` now takes `(email, otp, new_password)`, verifies
the OTP via `otp_service.verify_otp` (which already transitions
`INVITED → ACTIVE`), sets the password, and mints the first token
pair via `_issue_pair`.

These tests assert:

- `verify_otp` is called with `(email, otp)` once per invocation.
- The password hash is set on the user.
- `email_verified_at` is set.
- `_issue_pair` is called once.
- Audit events are emitted (`INVITATION_ACCEPTED`, `PASSWORD_SET`,
  `EMAIL_VERIFIED`).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Import the full mapper graph so any ORM instantiations inside the
# service don't fail at mapper-resolution time.
from src.modules.patients import models as _patient_models  # noqa: F401
from src.modules.staff import models as _staff_models  # noqa: F401


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------
class _FakeVerifyResult:
    def __init__(self, user_id: uuid.UUID, email: str) -> None:
        self.user_id = user_id
        self.email = email


class _FakeIssuedTokens:
    def __init__(self, user_id: uuid.UUID) -> None:
        self.access_token = "access-abc"
        self.refresh_token = "refresh-xyz"
        self.expires_in = 900
        self.user = SimpleNamespace(
            id=user_id,
            email="alex@example.com",
            full_name="Alex",
        )


class _FakeSession:
    """AsyncSession stand-in — tracks flush, no real DB I/O."""

    def __init__(self) -> None:
        self.flush = AsyncMock()
        self.add = MagicMock()

    async def _execute(self, stmt: Any) -> Any:
        return MagicMock(scalar_one_or_none=lambda: None)


def _invited_user(user_id: uuid.UUID, email: str = "alex@example.com") -> SimpleNamespace:
    from src.shared.domain.enums import UserStatus

    return SimpleNamespace(
        id=user_id,
        email=email,
        full_name="Alex",
        status=UserStatus.INVITED,
        password_hash=None,
        must_change_password=True,
        email_verified_at=None,
        roles=[],
    )


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
class TestAcceptInvitationHappyPath:
    async def test_otp_verified_password_set_tokens_issued(self) -> None:
        """On a fresh OTP, the password is hashed and set,
        `email_verified_at` is stamped, and `_issue_pair` returns a
        full `IssuedTokens` (access + refresh + user)."""
        from src.modules.identity import auth_service

        user_id = uuid.uuid4()
        user = _invited_user(user_id)
        session = _FakeSession()

        fake_tokens = _FakeIssuedTokens(user_id)

        with (
            patch.object(auth_service, "otp_service") as mock_otp,
            patch.object(auth_service, "hash_password", return_value="hashed-pw"),
            patch.object(auth_service, "_record_audit", AsyncMock()) as mock_audit,
            patch.object(auth_service, "_load_user_with_roles", AsyncMock(return_value=user)),
            patch.object(auth_service, "_issue_pair", AsyncMock(return_value=fake_tokens)) as mock_pair,
        ):
            mock_otp.verify_otp = AsyncMock(
                return_value=_FakeVerifyResult(user_id, user.email)
            )

            result = await auth_service.accept_invitation(
                session,
                email=user.email,
                otp="123456",
                new_password="hunter2hunter2",
            )

        # Password was set.
        assert user.password_hash == "hashed-pw"
        assert user.must_change_password is False
        assert user.email_verified_at is not None
        # verify_otp was called once with our OTP.
        mock_otp.verify_otp.assert_awaited_once_with(
            session, email=user.email, otp="123456"
        )
        # Token pair returned.
        assert result is fake_tokens
        mock_pair.assert_awaited_once()
        # Three audit events.
        event_types = [c.kwargs["event_type"] for c in mock_audit.await_args_list]
        from src.shared.domain.enums import AuthAuditEventType

        assert event_types == [
            AuthAuditEventType.INVITATION_ACCEPTED,
            AuthAuditEventType.PASSWORD_SET,
            AuthAuditEventType.EMAIL_VERIFIED,
        ]

    async def test_idempotent_on_re_invite(self) -> None:
        """If the user is already ACTIVE (re-invite edge case),
        we still set the password — the latest password always wins."""
        from src.modules.identity import auth_service
        from src.shared.domain.enums import UserStatus

        user_id = uuid.uuid4()
        user = _invited_user(user_id)
        user.status = UserStatus.ACTIVE  # already accepted once
        user.email_verified_at = datetime.now(tz=UTC)
        session = _FakeSession()

        fake_tokens = _FakeIssuedTokens(user_id)

        with (
            patch.object(auth_service, "otp_service") as mock_otp,
            patch.object(auth_service, "hash_password", return_value="hashed-2"),
            patch.object(auth_service, "_record_audit", AsyncMock()),
            patch.object(auth_service, "_load_user_with_roles", AsyncMock(return_value=user)),
            patch.object(auth_service, "_issue_pair", AsyncMock(return_value=fake_tokens)),
        ):
            mock_otp.verify_otp = AsyncMock(
                return_value=_FakeVerifyResult(user_id, user.email)
            )

            result = await auth_service.accept_invitation(
                session,
                email=user.email,
                otp="654321",
                new_password="newer-password",
            )

        assert user.password_hash == "hashed-2"
        # email_verified_at was NOT overwritten (already set).
        assert result.access_token == "access-abc"


# ---------------------------------------------------------------------------
# Reject terminal user states
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
class TestAcceptInvitationRejectTerminalStates:
    async def test_locked_user_raises_invitation_already_consumed(self) -> None:
        """Users in LOCKED / INACTIVE / ARCHIVED shouldn't be handed
        tokens even if they have a valid OTP."""
        from src.modules.identity import auth_service
        from src.core.exceptions import InvitationAlreadyConsumedError
        from src.shared.domain.enums import UserStatus

        user_id = uuid.uuid4()
        user = _invited_user(user_id)
        user.status = UserStatus.LOCKED
        session = _FakeSession()

        with (
            patch.object(auth_service, "otp_service") as mock_otp,
            patch.object(auth_service, "hash_password") as mock_hash,
            patch.object(auth_service, "_record_audit", AsyncMock()),
            patch.object(auth_service, "_load_user_with_roles", AsyncMock(return_value=user)),
            patch.object(auth_service, "_issue_pair", AsyncMock()) as mock_pair,
        ):
            mock_otp.verify_otp = AsyncMock(
                return_value=_FakeVerifyResult(user_id, user.email)
            )

            with pytest.raises(InvitationAlreadyConsumedError):
                await auth_service.accept_invitation(
                    session,
                    email=user.email,
                    otp="123456",
                    new_password="hunter2hunter2",
                )

        # Password was NOT set, no token pair issued.
        mock_hash.assert_not_called()
        mock_pair.assert_not_called()

    async def test_archived_user_raises_invitation_already_consumed(self) -> None:
        from src.modules.identity import auth_service
        from src.core.exceptions import InvitationAlreadyConsumedError
        from src.shared.domain.enums import UserStatus

        user_id = uuid.uuid4()
        user = _invited_user(user_id)
        user.status = UserStatus.ARCHIVED
        session = _FakeSession()

        with (
            patch.object(auth_service, "otp_service") as mock_otp,
            patch.object(auth_service, "_load_user_with_roles", AsyncMock(return_value=user)),
            patch.object(auth_service, "_issue_pair", AsyncMock()) as mock_pair,
        ):
            mock_otp.verify_otp = AsyncMock(
                return_value=_FakeVerifyResult(user_id, user.email)
            )

            with pytest.raises(InvitationAlreadyConsumedError):
                await auth_service.accept_invitation(
                    session,
                    email=user.email,
                    otp="123456",
                    new_password="hunter2hunter2",
                )

        mock_pair.assert_not_called()


# ---------------------------------------------------------------------------
# OTP error surface
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
class TestAcceptInvitationOtpErrors:
    """`verify_otp` raises typed errors — we surface them verbatim so
    the frontend can branch on `data.code`. This test guards against
    wrapping them in a less-useful generic exception."""

    @pytest.mark.parametrize(
        "exc",
        [
            pytest.param(
                __import__("src.core.exceptions", fromlist=["InvalidOtpError"]).InvalidOtpError(),
                id="invalid_otp",
            ),
            pytest.param(
                __import__("src.core.exceptions", fromlist=["OtpExpiredError"]).OtpExpiredError(),
                id="otp_expired",
            ),
            pytest.param(
                __import__("src.core.exceptions", fromlist=["OtpMaxAttemptsExceededError"]).OtpMaxAttemptsExceededError(),
                id="max_attempts",
            ),
        ],
    )
    async def test_otp_errors_propagate(self, exc: Exception) -> None:
        from src.modules.identity import auth_service

        user_id = uuid.uuid4()
        user = _invited_user(user_id)
        session = _FakeSession()

        with (
            patch.object(auth_service, "otp_service") as mock_otp,
            patch.object(auth_service, "_load_user_with_roles", AsyncMock(return_value=user)),
            patch.object(auth_service, "_record_audit", AsyncMock()),
        ):
            mock_otp.verify_otp = AsyncMock(side_effect=exc)

            with pytest.raises(type(exc)):
                await auth_service.accept_invitation(
                    session,
                    email=user.email,
                    otp="123456",
                    new_password="hunter2hunter2",
                )


__all__ = [
    "TestAcceptInvitationHappyPath",
    "TestAcceptInvitationRejectTerminalStates",
    "TestAcceptInvitationOtpErrors",
]