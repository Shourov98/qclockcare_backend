"""SUPER_ADMIN-only platform admin management endpoints."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response, status
from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.exceptions import ConflictError, DuplicateResourceError, NotFoundError
from src.core.security import hash_password
from src.modules.identity.dependencies import (
    CurrentAuth,
    get_session_with_auth,
    require_role,
)
from src.modules.identity.models import User, UserRoleAssignment
from src.modules.identity.schemas import _validate_password
from src.shared.domain.enums import UserRole, UserStatus
from src.shared.schemas.docs import standard_responses
from src.shared.schemas.pagination import PaginatedResponse, build_offset_response

router = APIRouter(prefix="/admin/admins", tags=["admin-admins"])

_SUPER_ADMIN_ONLY = [Depends(require_role(UserRole.SUPER_ADMIN))]


class PlatformAdminResponse(BaseModel):
    """One SUPER_ADMIN user for the global dashboard."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    email: EmailStr
    full_name: str
    phone: str | None
    status: UserStatus
    email_verified: bool
    role: UserRole = UserRole.SUPER_ADMIN
    created_at: datetime
    updated_at: datetime


class PlatformAdminListResponse(PaginatedResponse[PlatformAdminResponse]):
    """Paginated SUPER_ADMIN list."""

    pass


class PlatformAdminCreateRequest(BaseModel):
    """Create a login-ready SUPER_ADMIN account."""

    model_config = ConfigDict(extra="forbid")

    full_name: str = Field(min_length=1, max_length=255)
    email: EmailStr
    phone: str | None = Field(default=None, max_length=32)
    password: str = Field(min_length=12, max_length=128)

    @field_validator("full_name")
    @classmethod
    def _strip_full_name(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("must not be empty or whitespace-only")
        return stripped

    @field_validator("password")
    @classmethod
    def _password_policy(cls, value: str) -> str:
        return _validate_password(value)


class PlatformAdminUpdateRequest(BaseModel):
    """Partial update for a SUPER_ADMIN account."""

    model_config = ConfigDict(extra="forbid")

    full_name: str | None = Field(default=None, min_length=1, max_length=255)
    email: EmailStr | None = None
    phone: str | None = Field(default=None, max_length=32)
    status: UserStatus | None = None
    password: str | None = Field(default=None, min_length=12, max_length=128)

    @field_validator("full_name")
    @classmethod
    def _strip_full_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            raise ValueError("must not be empty or whitespace-only")
        return stripped

    @field_validator("password")
    @classmethod
    def _password_policy(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _validate_password(value)


def _to_admin_response(user: User) -> PlatformAdminResponse:
    return PlatformAdminResponse(
        id=user.id,
        email=user.email,
        full_name=user.full_name,
        phone=user.phone,
        status=user.status,
        email_verified=user.email_verified_at is not None,
        created_at=user.created_at,
        updated_at=user.updated_at,
    )


async def _get_super_admin_or_404(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    include_archived: bool = False,
) -> User:
    filters = [
        User.id == user_id,
        UserRoleAssignment.role == UserRole.SUPER_ADMIN,
        UserRoleAssignment.agency_id.is_(None),
    ]
    if not include_archived:
        filters.extend([User.deleted_at.is_(None), User.status != UserStatus.ARCHIVED])

    user = (
        await session.execute(
            select(User)
            .join(UserRoleAssignment, UserRoleAssignment.user_id == User.id)
            .where(*filters)
        )
    ).scalar_one_or_none()
    if user is None:
        raise NotFoundError(details={"resource": "platform_admin", "id": str(user_id)})
    return user


async def _active_super_admin_count(session: AsyncSession) -> int:
    return (
        await session.execute(
            select(func.count())
            .select_from(User)
            .join(UserRoleAssignment, UserRoleAssignment.user_id == User.id)
            .where(
                UserRoleAssignment.role == UserRole.SUPER_ADMIN,
                UserRoleAssignment.agency_id.is_(None),
                User.deleted_at.is_(None),
                User.status == UserStatus.ACTIVE,
            )
        )
    ).scalar_one()


@router.get(
    "",
    response_model=PlatformAdminListResponse,
    dependencies=_SUPER_ADMIN_ONLY,
    responses=standard_responses(include=[401, 403]),
)
async def list_platform_admins_endpoint(
    session: Annotated[AsyncSession, Depends(get_session_with_auth)],
    page: Annotated[int, Query(ge=1, le=10000)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
    search: Annotated[str | None, Query(max_length=255)] = None,
) -> PlatformAdminListResponse:
    """List SUPER_ADMIN users for the global dashboard."""
    filters = [
        UserRoleAssignment.role == UserRole.SUPER_ADMIN,
        UserRoleAssignment.agency_id.is_(None),
        User.deleted_at.is_(None),
        User.status != UserStatus.ARCHIVED,
    ]
    if search:
        pattern = f"%{search.strip()}%"
        filters.append(or_(User.full_name.ilike(pattern), User.email.ilike(pattern)))

    base = (
        select(User)
        .join(UserRoleAssignment, UserRoleAssignment.user_id == User.id)
        .where(*filters)
    )
    total = (await session.execute(select(func.count()).select_from(base.subquery()))).scalar_one()
    rows = (
        (
            await session.execute(
                base.order_by(User.created_at.desc(), User.id)
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
        )
        .scalars()
        .all()
    )
    body = build_offset_response(
        [_to_admin_response(user) for user in rows],
        total=total,
        page=page,
        page_size=page_size,
    )
    return PlatformAdminListResponse.model_validate(body)


@router.post(
    "",
    response_model=PlatformAdminResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=_SUPER_ADMIN_ONLY,
    responses=standard_responses(include=[401, 403, 409, 422]),
)
async def create_platform_admin_endpoint(
    payload: PlatformAdminCreateRequest,
    session: Annotated[AsyncSession, Depends(get_session_with_auth)],
) -> PlatformAdminResponse:
    """Create a login-ready SUPER_ADMIN user."""
    existing = (
        await session.execute(select(User).where(User.email == payload.email))
    ).scalar_one_or_none()
    if existing is not None:
        raise DuplicateResourceError(
            message="A user with this email already exists.",
            details={"email": str(payload.email)},
        )

    user = User(
        email=str(payload.email),
        full_name=payload.full_name,
        phone=payload.phone,
        status=UserStatus.ACTIVE,
        password_hash=hash_password(payload.password),
        email_verified_at=datetime.now(tz=UTC),
        must_change_password=False,
    )
    session.add(user)
    await session.flush()
    session.add(
        UserRoleAssignment(
            user_id=user.id,
            role=UserRole.SUPER_ADMIN,
            agency_id=None,
        )
    )
    await session.commit()
    await session.refresh(user)
    return _to_admin_response(user)


@router.get(
    "/{user_id}",
    response_model=PlatformAdminResponse,
    dependencies=_SUPER_ADMIN_ONLY,
    responses=standard_responses(include=[401, 403, 404]),
)
async def get_platform_admin_endpoint(
    user_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session_with_auth)],
) -> PlatformAdminResponse:
    """Fetch one SUPER_ADMIN user."""
    user = await _get_super_admin_or_404(session, user_id=user_id)
    return _to_admin_response(user)


@router.patch(
    "/{user_id}",
    response_model=PlatformAdminResponse,
    dependencies=_SUPER_ADMIN_ONLY,
    responses=standard_responses(include=[401, 403, 404, 409, 422]),
)
async def update_platform_admin_endpoint(
    user_id: uuid.UUID,
    payload: PlatformAdminUpdateRequest,
    ctx: CurrentAuth,
    session: Annotated[AsyncSession, Depends(get_session_with_auth)],
) -> PlatformAdminResponse:
    """Update a SUPER_ADMIN user."""
    user = await _get_super_admin_or_404(session, user_id=user_id, include_archived=True)
    changes = payload.model_dump(exclude_unset=True)

    if "email" in changes and changes["email"] != user.email:
        existing = (
            await session.execute(select(User).where(User.email == changes["email"]))
        ).scalar_one_or_none()
        if existing is not None and existing.id != user.id:
            raise DuplicateResourceError(
                message="A user with this email already exists.",
                details={"email": str(changes["email"])},
            )
        user.email = str(changes["email"])

    if "full_name" in changes:
        user.full_name = changes["full_name"]
    if "phone" in changes:
        user.phone = changes["phone"]
    if "status" in changes:
        next_status = changes["status"]
        if user.id == ctx.user_id and next_status != UserStatus.ACTIVE:
            raise ConflictError(
                message="You cannot disable your own super admin account.",
                details={"user_id": str(user_id)},
            )
        if (
            user.status == UserStatus.ACTIVE
            and next_status != UserStatus.ACTIVE
            and await _active_super_admin_count(session) <= 1
        ):
            raise ConflictError(
                message="At least one active super admin account must remain.",
                details={"user_id": str(user_id)},
            )
        user.status = next_status
        if next_status == UserStatus.ARCHIVED:
            user.deleted_at = datetime.now(tz=UTC)
        elif user.deleted_at is not None:
            user.deleted_at = None
    if "password" in changes:
        user.password_hash = hash_password(changes["password"])
        user.email_verified_at = user.email_verified_at or datetime.now(tz=UTC)

    await session.commit()
    await session.refresh(user)
    return _to_admin_response(user)


@router.delete(
    "/{user_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=_SUPER_ADMIN_ONLY,
    responses=standard_responses(include=[401, 403, 404, 409]),
)
async def archive_platform_admin_endpoint(
    user_id: uuid.UUID,
    ctx: CurrentAuth,
    session: Annotated[AsyncSession, Depends(get_session_with_auth)],
) -> Response:
    """Archive a SUPER_ADMIN user so they can no longer sign in."""
    user = await _get_super_admin_or_404(session, user_id=user_id)
    if user.id == ctx.user_id:
        raise ConflictError(
            message="You cannot archive your own super admin account.",
            details={"user_id": str(user_id)},
        )
    if await _active_super_admin_count(session) <= 1:
        raise ConflictError(
            message="At least one active super admin account must remain.",
            details={"user_id": str(user_id)},
        )

    user.status = UserStatus.ARCHIVED
    user.deleted_at = datetime.now(tz=UTC)
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
