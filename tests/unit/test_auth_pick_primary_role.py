"""Unit tests for `auth_service._pick_primary_role`.

The function picks the most-privileged role out of a user's role rows
to attach to the issued access token. The selected role drives *every*
downstream authorization check (RLS, route guards, JWT claims), so a
mistake here turns a SUPER_ADMIN into a non-entity or vice versa.

Regression coverage for the bug where a user with zero role rows was
silently downgraded to STAFF with no agency — issuing a token that
passed auth but failed every protected route.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from src.core.exceptions import InsufficientPermissionsError
from src.modules.identity.auth_service import _pick_primary_role
from src.shared.domain.enums import UserRole


def _role(role: UserRole, agency_id: uuid.UUID | None = None) -> SimpleNamespace:
    return SimpleNamespace(role=role, agency_id=agency_id)


# --------------------------------------------------------------------------
# Happy paths
# --------------------------------------------------------------------------
def test_super_admin_wins_agency_id_is_none() -> None:
    """A SUPER_ADMIN role row — even mixed with downgraded roles — wins."""
    roles = [
        _role(UserRole.STAFF, agency_id=uuid.uuid4()),
        _role(UserRole.SUPER_ADMIN),
    ]
    role, agency_id = _pick_primary_role(roles)
    assert role is UserRole.SUPER_ADMIN
    assert agency_id is None


def test_agency_admin_chosen_when_no_super_admin() -> None:
    """AGENCY_ADMIN outranks STAFF / PATIENT when SUPER_ADMIN is absent."""
    agency = uuid.uuid4()
    roles = [
        _role(UserRole.STAFF, agency_id=agency),
        _role(UserRole.PATIENT, agency_id=agency),
        _role(UserRole.AGENCY_ADMIN, agency_id=agency),
    ]
    role, agency_id = _pick_primary_role(roles)
    assert role is UserRole.AGENCY_ADMIN
    assert agency_id == agency


def test_staff_chosen_when_only_staff() -> None:
    agency = uuid.uuid4()
    roles = [_role(UserRole.STAFF, agency_id=agency)]
    role, agency_id = _pick_primary_role(roles)
    assert role is UserRole.STAFF
    assert agency_id == agency


def test_patient_chosen_when_only_patient() -> None:
    agency = uuid.uuid4()
    roles = [_role(UserRole.PATIENT, agency_id=agency)]
    role, agency_id = _pick_primary_role(roles)
    assert role is UserRole.PATIENT
    assert agency_id == agency


# --------------------------------------------------------------------------
# Regression: empty role list must NOT silently fall back to STAFF
# --------------------------------------------------------------------------
def test_no_roles_raises_insufficient_permissions() -> None:
    """The fix: a user with zero role rows can no longer log in.

    Previously the function returned `(UserRole.STAFF, None)` as a
    fallback, which silently issued a token claiming the user was a
    STAFF with no agency. Downstream RLS policies then rejected every
    query as not-agency-scoped, while `/auth/me` happily reported the
    user as a STAFF. The wrong role also unlocked the global dashboards
    for *any* orphaned user. The fix is to refuse the token entirely.
    """
    user_id = uuid.uuid4()
    with pytest.raises(InsufficientPermissionsError) as exc:
        _pick_primary_role([], user_id=user_id)
    assert "No role assigned" in str(exc.value)
    assert exc.value.details == {"user_id": str(user_id)}


def test_no_roles_raises_even_without_user_id() -> None:
    """The user_id is optional — the error still fires without it."""
    with pytest.raises(InsufficientPermissionsError):
        _pick_primary_role([])


def test_no_roles_logs_critical_event(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The error path must emit a structured `auth.pick_primary_role.no_roles`
    log so on-call sees the data-integrity problem in the dashboards.

    `capsys` is used (not `caplog`) because the project's structlog
    config writes to stdout, not to the stdlib `logging` root.
    """
    with pytest.raises(InsufficientPermissionsError):
        _pick_primary_role([], user_id=uuid.uuid4())
    captured = capsys.readouterr().out + capsys.readouterr().err
    assert "auth.pick_primary_role.no_roles" in captured


__all__: list[str] = []
