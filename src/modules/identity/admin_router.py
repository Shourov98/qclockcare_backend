"""SUPER_ADMIN-only platform admin management endpoints.

This router handles both SUPER_ADMIN and PLATFORM_ADMIN users. They
share the same "admin" namespace in the UI but are distinct roles:
  - SUPER_ADMIN: full cross-tenant access. Created via seed scripts;
    the UI does NOT expose a creation form.
  - PLATFORM_ADMIN: created via `POST /admin/admins` with a list of
    AdminScope values. Holds scoped cross-tenant access.

Endpoints:
  GET    /admin/admins           — list both roles
  GET    /admin/admins/{id}      — fetch one (either role)
  POST   /admin/admins           — create PLATFORM_ADMIN with scopes
                                   (SUPER_ADMIN only)
  PATCH  /admin/admins/{id}      — partial update (both roles)
  DELETE /admin/admins/{id}      — archive (SUPER_ADMIN only)
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, Query, Request, Response, status
from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.config import settings
from src.core.exceptions import ConflictError, DuplicateResourceError, NotFoundError
from src.modules.audit_logs import service as audit_logs_service
from src.modules.identity.dependencies import (
    CurrentAuth,
    get_session_with_auth,
    require_role,
)
from src.modules.identity.models import AdminScope as AdminScopeModel
from src.modules.identity.models import User, UserRoleAssignment
from src.modules.identity.schemas import _validate_password
from src.shared.domain.enums import AdminScope, AuditAction, UserRole, UserStatus
from src.shared.schemas.docs import standard_responses
from src.shared.schemas.pagination import PaginatedResponse, build_offset_response

router = APIRouter(prefix="/admin/admins", tags=["admin-admins"])

_SUPER_ADMIN_ONLY = [Depends(require_role(UserRole.SUPER_ADMIN))]


class PlatformAdminResponse(BaseModel):
    """One SUPER_ADMIN or PLATFORM_ADMIN user for the global dashboard."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    email: EmailStr
    full_name: str
    phone: str | None
    status: UserStatus
    email_verified: bool
    role: UserRole
    scopes: list[AdminScope] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime


class PlatformAdminListResponse(PaginatedResponse[PlatformAdminResponse]):
    """Paginated admin list (SUPER_ADMIN + PLATFORM_ADMIN)."""

    pass


class PlatformAdminCreateRequest(BaseModel):
    """Create a PLATFORM_ADMIN account with one or more scopes.

    On success the recipient receives an invitation email with a magic
    link (same flow as staff invitations). They set their password
    on first visit; the user is in `INVITED` status until then.
    """

    model_config = ConfigDict(extra="forbid")

    full_name: str = Field(min_length=1, max_length=255)
    email: EmailStr
    phone: str | None = Field(default=None, max_length=32)
    scopes: list[AdminScope] = Field(
        min_length=1,
        description="At least one scope is required.",
    )

    @field_validator("full_name")
    @classmethod
    def _strip_full_name(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("must not be empty or whitespace-only")
        return stripped


class PlatformAdminUpdateRequest(BaseModel):
    """Partial update for an admin account (SUPER_ADMIN or PLATFORM_ADMIN).

    `scopes` only applies when updating a PLATFORM_ADMIN; ignored
    otherwise (a SUPER_ADMIN's permissions come from the role, not
    from `admin_scopes`).
    """

    model_config = ConfigDict(extra="forbid")

    full_name: str | None = Field(default=None, min_length=1, max_length=255)
    email: EmailStr | None = None
    phone: str | None = Field(default=None, max_length=32)
    status: UserStatus | None = None
    password: str | None = Field(default=None, min_length=12, max_length=128)
    scopes: list[AdminScope] | None = None

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


def _to_admin_response(
    user: User,
    *,
    role: UserRole,
    scopes: list[AdminScope] | None = None,
) -> PlatformAdminResponse:
    return PlatformAdminResponse(
        id=user.id,
        email=user.email,
        full_name=user.full_name,
        phone=user.phone,
        status=user.status,
        email_verified=user.email_verified_at is not None,
        role=role,
        scopes=scopes or [],
        created_at=user.created_at,
        updated_at=user.updated_at,
    )


async def _get_admin_or_404(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    include_archived: bool = False,
) -> tuple[User, UserRole, list[AdminScope]]:
    """Fetch an admin user (SUPER_ADMIN or PLATFORM_ADMIN) by id.

    Returns (user, role, scopes). `scopes` is non-empty only for
    PLATFORM_ADMIN; empty for SUPER_ADMIN.
    """
    # Step 1: find which role assignment this user holds. Both roles
    # are stored with agency_id = NULL.
    role_rows = (
        await session.execute(
            select(UserRoleAssignment.role)
            .where(
                UserRoleAssignment.user_id == user_id,
                UserRoleAssignment.role.in_(
                    [UserRole.SUPER_ADMIN, UserRole.PLATFORM_ADMIN]
                ),
                UserRoleAssignment.agency_id.is_(None),
            )
        )
    ).scalars().all()

    if not role_rows:
        raise NotFoundError(details={"resource": "platform_admin", "id": str(user_id)})

    # Pick the higher-privilege role if both somehow exist (shouldn't
    # happen — a user has only one role row in this codebase — but
    # defend against it).
    role = UserRole.SUPER_ADMIN if UserRole.SUPER_ADMIN in role_rows else UserRole.PLATFORM_ADMIN

    filters = [User.id == user_id]
    if not include_archived:
        filters.extend(
            [User.deleted_at.is_(None), User.status != UserStatus.ARCHIVED]
        )
    user = (
        await session.execute(select(User).where(*filters))
    ).scalar_one_or_none()
    if user is None:
        raise NotFoundError(
            details={"resource": "platform_admin", "id": str(user_id)}
        )

    scopes: list[AdminScope] = []
    if role == UserRole.PLATFORM_ADMIN:
        scope_names = (
            await session.execute(
                select(AdminScopeModel.scope_name).where(
                    AdminScopeModel.user_id == user_id
                )
            )
        ).scalars().all()
        scopes = [AdminScope(s) for s in scope_names]

    return user, role, scopes


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
    """List SUPER_ADMIN and PLATFORM_ADMIN users for the global dashboard.

    SUPER_ADMIN users do not need scope rows — they always have full
    access — so the response surfaces their `scopes` field as `[]`.
    """
    filters = [
        UserRoleAssignment.role.in_(
            [UserRole.SUPER_ADMIN, UserRole.PLATFORM_ADMIN]
        ),
        UserRoleAssignment.agency_id.is_(None),
        User.deleted_at.is_(None),
        User.status != UserStatus.ARCHIVED,
    ]
    if search:
        pattern = f"%{search.strip()}%"
        filters.append(or_(User.full_name.ilike(pattern), User.email.ilike(pattern)))

    base = (
        select(User, UserRoleAssignment.role)
        .join(UserRoleAssignment, UserRoleAssignment.user_id == User.id)
        .where(*filters)
        .order_by(User.created_at.desc(), User.id)
    )
    total = (
        await session.execute(select(func.count()).select_from(base.subquery()))
    ).scalar_one()
    rows = (
        (
            await session.execute(
                base.offset((page - 1) * page_size).limit(page_size)
            )
        )
        .all()
    )

    # Fetch scopes for all PLATFORM_ADMIN rows in one query.
    user_ids = [r.User.id for r in rows]
    scope_by_user: dict[uuid.UUID, list[AdminScope]] = {}
    if user_ids:
        scope_rows = (
            await session.execute(
                select(AdminScopeModel.user_id, AdminScopeModel.scope_name).where(
                    AdminScopeModel.user_id.in_(user_ids)
                )
            )
        ).all()
        for uid, name in scope_rows:
            scope_by_user.setdefault(uid, []).append(AdminScope(name))

    items = [
        _to_admin_response(
            r.User,
            role=r.role,
            scopes=scope_by_user.get(r.User.id, []),
        )
        for r in rows
    ]
    body = build_offset_response(
        items, total=total, page=page, page_size=page_size
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
    request: Request,
    background_tasks: BackgroundTasks,
    ctx: CurrentAuth,
    session: Annotated[AsyncSession, Depends(get_session_with_auth)],
) -> PlatformAdminResponse:
    """Create a PLATFORM_ADMIN account with the given scopes.

    Recipients receive an invitation email (same magic-link flow as
    staff invitations). Their account starts in `INVITED` status and
    transitions to `EMAIL_VERIFICATION_PENDING` → `ACTIVE` as they
    set their password and verify their email.

    Scope assignment writes one row per scope to `admin_scopes` with
    `granted_by = ctx.user_id` so we have an audit trail of who issued
    the scope.
    """
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
        status=UserStatus.INVITED,
        must_change_password=False,
    )
    session.add(user)
    await session.flush()

    session.add(
        UserRoleAssignment(
            user_id=user.id,
            role=UserRole.PLATFORM_ADMIN,
            agency_id=None,
        )
    )
    for scope in payload.scopes:
        session.add(
            AdminScopeModel(
                user_id=user.id,
                scope_name=scope.value,
                granted_by=ctx.user_id,
            )
        )

    # Issue invitation OTP + dispatch email via BackgroundTasks so the
    # response returns immediately. Reuses the same `otp_service` and
    # `auth_email.send_invitation_email` helper as the AGENCY_ADMIN
    # invite flow so the user experience is identical.
    from src.modules.identity import otp_service
    from src.modules.auth import email_service as auth_email

    issued = await otp_service.issue_otp(session, user=user)
    await session.commit()
    await session.refresh(user)

    auth_email.send_invitation_email(
        background_tasks,
        to_email=str(user.email),
        to_name=user.full_name,
        otp=issued.otp,
        expires_in_minutes=settings.OTP_EXPIRY_MINUTES,
        recipient_user_id=user.id,
    )

    # Best-effort audit row.
    try:
        ip, ua = audit_logs_service.request_ip_ua(request)
        await audit_logs_service.audit_log(
            session,
            agency_id=None,
            actor_user_id=ctx.user_id,
            action=AuditAction.ROLE_GRANTED,
            entity_type="ADMIN_USER",
            entity_id=user.id,
            new_data={
                "email": str(user.email),
                "role": UserRole.PLATFORM_ADMIN.value,
                "scopes": [s.value for s in payload.scopes],
            },
            ip_address=ip,
            user_agent=ua,
        )
        await session.commit()
    except Exception:
        pass

    return _to_admin_response(
        user, role=UserRole.PLATFORM_ADMIN, scopes=payload.scopes
    )


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
    """Fetch one admin (SUPER_ADMIN or PLATFORM_ADMIN) by id."""
    user, role, scopes = await _get_admin_or_404(session, user_id=user_id)
    return _to_admin_response(user, role=role, scopes=scopes)


@router.patch(
    "/{user_id}",
    response_model=PlatformAdminResponse,
    dependencies=_SUPER_ADMIN_ONLY,
    responses=standard_responses(include=[401, 403, 404, 409, 422]),
)
async def update_platform_admin_endpoint(
    user_id: uuid.UUID,
    payload: PlatformAdminUpdateRequest,
    request: Request,
    ctx: CurrentAuth,
    session: Annotated[AsyncSession, Depends(get_session_with_auth)],
) -> PlatformAdminResponse:
    """Update an admin user.

    Editable fields:
      - full_name, email, phone, status, password
      - scopes (PLATFORM_ADMIN only)

    SUPER_ADMINs cannot have their scopes edited here — their access
    is governed by the role itself, not by `admin_scopes`. Sending
    `scopes` on a SUPER_ADMIN edit is silently ignored.
    """
    user, role, current_scopes = await _get_admin_or_404(
        session, user_id=user_id, include_archived=True
    )
    changes = payload.model_dump(exclude_unset=True)

    if "email" in changes and changes["email"] != user.email:
        existing = (
            await session.execute(
                select(User).where(User.email == changes["email"])
            )
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
                message="You cannot disable your own admin account.",
                details={"user_id": str(user_id)},
            )
        if role == UserRole.SUPER_ADMIN:
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
        from src.core.security import hash_password as _hash_password

        user.password_hash = _hash_password(changes["password"])
        user.email_verified_at = user.email_verified_at or datetime.now(tz=UTC)
    if "scopes" in changes and role == UserRole.PLATFORM_ADMIN:
        new_scopes = set(changes["scopes"])  # type: ignore[arg-type]
        existing_scopes = set(current_scopes)
        # Add new scopes.
        for scope in new_scopes - existing_scopes:
            session.add(
                AdminScopeModel(
                    user_id=user.id,
                    scope_name=scope.value,
                    granted_by=ctx.user_id,
                )
            )
        # Remove revoked scopes.
        if existing_scopes - new_scopes:
            await session.execute(
                AdminScopeModel.__table__.delete().where(
                    AdminScopeModel.user_id == user.id,
                    AdminScopeModel.scope_name.in_(
                        [s.value for s in (existing_scopes - new_scopes)]
                    ),
                )
            )
        current_scopes = [
            AdminScope(s) for s in (new_scopes)
        ]

    await session.commit()
    await session.refresh(user)
    # Best-effort audit row.
    try:
        ip, ua = audit_logs_service.request_ip_ua(request)
        await audit_logs_service.audit_log(
            session,
            agency_id=None,
            actor_user_id=ctx.user_id,
            action=AuditAction.UPDATE,
            entity_type="ADMIN_USER",
            entity_id=user.id,
            new_data={"fields": sorted(changes.keys())},
            metadata={"role": role.value, "scopes": [s.value for s in current_scopes]},
            ip_address=ip,
            user_agent=ua,
        )
        await session.commit()
    except Exception:
        pass
    return _to_admin_response(user, role=role, scopes=current_scopes)


@router.delete(
    "/{user_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=_SUPER_ADMIN_ONLY,
    responses=standard_responses(include=[401, 403, 404, 409]),
)
async def archive_platform_admin_endpoint(
    user_id: uuid.UUID,
    request: Request,
    ctx: CurrentAuth,
    session: Annotated[AsyncSession, Depends(get_session_with_auth)],
) -> Response:
    """Archive an admin user so they can no longer sign in.

    SUPER_ADMIN accounts enforce "at least one active" — but
    PLATFORM_ADMIN accounts do not.
    """
    user, role, _ = await _get_admin_or_404(session, user_id=user_id)
    if user.id == ctx.user_id:
        raise ConflictError(
            message="You cannot archive your own admin account.",
            details={"user_id": str(user_id)},
        )
    if (
        role == UserRole.SUPER_ADMIN
        and await _active_super_admin_count(session) <= 1
    ):
        raise ConflictError(
            message="At least one active super admin account must remain.",
            details={"user_id": str(user_id)},
        )

    user.status = UserStatus.ARCHIVED
    user.deleted_at = datetime.now(tz=UTC)
    await session.commit()
    # Best-effort audit row.
    try:
        ip, ua = audit_logs_service.request_ip_ua(request)
        await audit_logs_service.audit_log(
            session,
            agency_id=None,
            actor_user_id=ctx.user_id,
            action=AuditAction.DELETE,
            entity_type="ADMIN_USER",
            entity_id=user.id,
            old_data={"status": UserStatus.ACTIVE.value, "email": str(user.email)},
            metadata={"role": role.value},
            ip_address=ip,
            user_agent=ua,
        )
        await session.commit()
    except Exception:
        pass
    return Response(status_code=status.HTTP_204_NO_CONTENT)
