"""Scope-based dependencies for PLATFORM_ADMIN RBAC.

`require_scope(scope)` and `require_any_scope(*scopes)` are layered on
top of `require_role(...)`. They accept:
  - SUPER_ADMIN unconditionally (full cross-tenant access)
  - PLATFORM_ADMIN if they hold the required scope(s) in their JWT
    claim (set at issue time by `_load_user_scopes` in auth_service)
  - Everyone else → 403

The dependency reads from `AuthContext.scopes` populated by
`get_session_with_auth` from the JWT — no DB roundtrip per request.

Usage:
    @router.get("/agencies", dependencies=[Depends(require_scope(AdminScope.AGENCIES))])
    async def list_agencies(...): ...
"""

from __future__ import annotations

from collections.abc import Callable

from src.core.exceptions import InsufficientPermissionsError
from src.modules.identity.dependencies import AuthContext, CurrentAuth
from src.shared.domain.enums import AdminScope, UserRole


def require_scope(scope: AdminScope) -> Callable[[AuthContext], AuthContext]:
    """Build a dependency that asserts the caller holds `scope`.

    SUPER_ADMIN passes unconditionally. PLATFORM_ADMIN must have the
    scope in their JWT claim. AGENCY_ADMIN/STAFF/PATIENT/GUARDIAN get
    403 — they are not cross-tenant admins.
    """

    def _check(ctx: CurrentAuth) -> AuthContext:
        if ctx.role == UserRole.SUPER_ADMIN:
            return ctx
        if ctx.role == UserRole.PLATFORM_ADMIN and scope.value in ctx.scopes:
            return ctx
        raise InsufficientPermissionsError(
            message=f"Missing required scope: {scope.value}",
            details={"required_scope": scope.value, "role": ctx.role.value},
        )

    return _check


def require_any_scope(*scopes: AdminScope) -> Callable[[AuthContext], AuthContext]:
    """Build a dependency that asserts the caller holds at least one scope.

    Useful for endpoints that are useful across multiple unrelated
    scopes (e.g. a "search" endpoint that AGENCIES+CLINICAL+SUPPORT
    admins might all use).
    """
    if not scopes:
        raise ValueError("require_any_scope needs at least one scope")

    scope_values = frozenset(s.value for s in scopes)

    def _check(ctx: CurrentAuth) -> AuthContext:
        if ctx.role == UserRole.SUPER_ADMIN:
            return ctx
        if ctx.role == UserRole.PLATFORM_ADMIN and (
            scope_values & set(ctx.scopes)
        ):
            return ctx
        raise InsufficientPermissionsError(
            message=f"Missing required scope (any of: {sorted(scope_values)})",
            details={"required_any_of": sorted(scope_values), "role": ctx.role.value},
        )

    return _check


def require_admin() -> Callable[[AuthContext], AuthContext]:
    """Shorthand: caller must be SUPER_ADMIN or PLATFORM_ADMIN.

    For endpoints that both admin tiers should reach (e.g. listing
    admin users). Does NOT check scopes — pair with `require_scope`
    if scope gating is needed.
    """
    from src.modules.identity.dependencies import require_role  # noqa: F401

    def _check(ctx: CurrentAuth) -> AuthContext:
        if ctx.role in {UserRole.SUPER_ADMIN, UserRole.PLATFORM_ADMIN}:
            return ctx
        raise InsufficientPermissionsError(
            message="Cross-tenant admin role required.",
            details={"role": ctx.role.value},
        )

    return _check


__all__ = [
    "require_admin",
    "require_any_scope",
    "require_scope",
]
