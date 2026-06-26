"""Auth dependencies — request-level authentication + RLS context.

`get_current_user` is the FastAPI dependency that:
  1. Reads the bearer token from `Authorization: Bearer <jwt>`
  2. Verifies it via `jwt_service.verify_access_token`
  3. Loads the user from the DB (forcing the session to enforce RLS)
  4. Sets the RLS session vars (current_user_id / current_agency_id /
     current_user_role) for the request's transactional session

`require_role(*roles)` builds on top to enforce a specific role.

The token + user are attached to `request.state` so route handlers can
access them without re-validating.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.database import get_session, set_session_context
from src.modules.identity import jwt_service
from src.modules.identity.models import User
from src.modules.identity.schemas import CurrentUser
from src.shared.domain.enums import UserRole, UserStatus

# `auto_error=False` lets us return our own 401 with the project's error
# envelope (instead of FastAPI's default 403).
_bearer = HTTPBearer(auto_error=False)


@dataclass(slots=True)
class AuthContext:
    """Everything the request handler needs about the caller."""

    user_id: uuid.UUID
    user: CurrentUser
    role: UserRole
    agency_id: uuid.UUID | None
    raw_token: str


# --------------------------------------------------------------------------
# DB session dependency that ALSO sets RLS GUCs.
# --------------------------------------------------------------------------
async def get_session_with_auth(
    request: Request,
    credentials: Annotated[
        HTTPAuthorizationCredentials | None, Depends(_bearer)
    ] = None,
) -> AsyncIterator[AsyncSession]:
    """Same as `get_session`, but additionally sets RLS GUCs from the bearer.

    If `credentials` is present, we verify the token, look up the user,
    and call `set_session_context`. The session is committed on success
    and rolled back on exception (inherited from `get_session`).
    """
    async for session in get_session():
        if credentials is not None:
            payload = jwt_service.verify_access_token(credentials.credentials)
            user = (
                await session.execute(
                    select(User).where(User.id == payload.user_id)
                )
            ).scalar_one_or_none()
            if user is None:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="User no longer exists.",
                )
            if user.status in {UserStatus.INACTIVE, UserStatus.ARCHIVED}:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Account disabled.",
                )
            # Attach the auth context to the request for downstream handlers
            from src.modules.identity.auth_service import _to_current_user, _pick_primary_role

            role, agency_id = _pick_primary_role(user.roles)
            request.state.auth = AuthContext(
                user_id=user.id,
                user=_to_current_user(user),
                role=role,
                agency_id=agency_id,
                raw_token=credentials.credentials,
            )
            await set_session_context(
                session,
                user_id=str(user.id),
                agency_id=str(agency_id) if agency_id else None,
                user_role=role.value,
            )
        yield session


# --------------------------------------------------------------------------
# require_current_user — strict variant, must be authenticated
# --------------------------------------------------------------------------
def get_current_auth(
    request: Request,
) -> AuthContext:
    """Return the AuthContext populated by `get_session_with_auth`.

    Use this AFTER `get_session_with_auth` in the dependency chain. It will
    raise 401 if no auth context was attached.
    """
    ctx = getattr(request.state, "auth", None)
    if ctx is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required.",
        )
    return ctx


CurrentAuth = Annotated[AuthContext, Depends(get_current_auth)]


# --------------------------------------------------------------------------
# require_role — gate a route on one or more roles
# --------------------------------------------------------------------------
def require_role(*roles: UserRole) -> Callable[[AuthContext], AuthContext]:
    """Build a dependency that asserts the caller has one of the given roles.

    Usage:
        @router.post(..., dependencies=[Depends(require_role(UserRole.AGENCY_ADMIN))])
    """

    role_values = {r.value for r in roles}

    def _check(ctx: CurrentAuth) -> AuthContext:
        if ctx.role.value not in role_values:
            # 403 with the project's envelope is handled by the global handler
            from src.core.exceptions import InsufficientPermissionsError

            raise InsufficientPermissionsError(
                details={"required_any_of": sorted(role_values)}
            )
        return ctx

    return _check


__all__ = [
    "AuthContext",
    "CurrentAuth",
    "get_current_auth",
    "get_session_with_auth",
    "require_role",
]