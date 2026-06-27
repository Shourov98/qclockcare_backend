"""Patients router — `/patients`, `/guardians`, and relationships.

All routes require authentication. Write routes require AGENCY_ADMIN.
Read routes allow the patient / guardian themselves (via `_ensure_can_view`).

Endpoints:
  POST   /patients                                       — admit patient
  GET    /patients                                       — list (paginated)
  GET    /patients/{id}                                  — fetch (summary)
  GET    /patients/{id}/with-relationships               — fetch + guardian links
  PATCH  /patients/{id}                                  — update
  DELETE /patients/{id}                                  — archive

  GET    /patients/{id}/guardians                        — list guardians
  POST   /patients/{id}/guardians                        — link (existing or new)

  GET    /guardians                                      — list
  POST   /guardians                                      — create standalone
  GET    /guardians/{id}                                 — fetch
  PATCH  /guardians/{id}                                 — update
  DELETE /guardians/{id}                                 — archive

  PATCH  /patient-guardian-relationships/{id}            — edit a link
  DELETE /patient-guardian-relationships/{id}            — remove a link
"""

from __future__ import annotations

import contextlib
import uuid
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, Query, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.config import settings
from src.core.exceptions import CrossAgencyAccessDeniedError, ForbiddenError
from src.core.logging import get_logger
from src.modules.audit_logs import service as audit_logs_service
from src.modules.auth import email_service as auth_email
from src.modules.identity.dependencies import (
    CurrentAuth,
    get_session_with_auth,
    require_role,
)
from src.modules.patients import service as patients_service
from src.modules.patients.schemas import (
    GuardianProfileCreateRequest,
    GuardianProfileResponse,
    GuardianProfileUpdateRequest,
    PatientGuardianRelationshipCreateRequest,
    PatientGuardianRelationshipResponse,
    PatientGuardianRelationshipUpdateRequest,
    PatientProfileCreateRequest,
    PatientProfileResponse,
    PatientProfileSummaryResponse,
    PatientProfileUpdateRequest,
)
from src.shared.domain.enums import AuditAction, UserRole, UserStatus
from src.shared.schemas.pagination import build_offset_response

logger = get_logger(__name__)

router = APIRouter(tags=["patients"])


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def _require_agency(ctx: CurrentAuth) -> uuid.UUID:
    """AGENCY_ADMIN / STAFF / PATIENT / GUARDIAN must have an agency."""
    if ctx.role == UserRole.SUPER_ADMIN:
        raise ForbiddenError(
            "Use the platform admin console for cross-agency patient operations."
        )
    if ctx.agency_id is None:
        raise ForbiddenError("Caller has no agency context.")
    return ctx.agency_id


def _ensure_can_view_patient(ctx: CurrentAuth, patient_user_id: uuid.UUID) -> None:
    """AGENCY_ADMIN at the agency, or the patient themselves."""
    if ctx.role == UserRole.SUPER_ADMIN:
        return
    if ctx.role == UserRole.AGENCY_ADMIN:
        return
    if ctx.user_id == patient_user_id:
        return
    raise CrossAgencyAccessDeniedError()


def _ensure_can_view_guardian(ctx: CurrentAuth, guardian_user_id: uuid.UUID) -> None:
    if ctx.role == UserRole.SUPER_ADMIN:
        return
    if ctx.role == UserRole.AGENCY_ADMIN:
        return
    if ctx.user_id == guardian_user_id:
        return
    raise CrossAgencyAccessDeniedError()


def _to_patient_response(
    patient: object,
    *,
    with_relationships: bool = False,
) -> PatientProfileResponse:
    data: dict = {
        "id": patient.id,
        "agency_id": patient.agency_id,
        "user_id": patient.user_id,
        "patient_code": patient.patient_code,
        "status": patient.status,
        "date_of_birth": patient.date_of_birth,
        "gender": patient.gender,
        "preferred_language": patient.preferred_language,
        "care_notes": patient.care_notes,
        "admitted_at": patient.admitted_at,
        "discharged_at": patient.discharged_at,
        "created_at": patient.created_at,
        "updated_at": patient.updated_at,
    }
    if with_relationships:
        try:
            data["guardian_links"] = list(patient.guardian_links)
        except Exception:
            data["guardian_links"] = None
    else:
        data["guardian_links"] = None
    return PatientProfileResponse.model_validate(data)


# --------------------------------------------------------------------------
# Patient profiles
# --------------------------------------------------------------------------
@router.post(
    "/patients",
    response_model=PatientProfileResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_role(UserRole.AGENCY_ADMIN))],
)
async def create_patient_endpoint(
    payload: PatientProfileCreateRequest,
    request: Request,
    background_tasks: BackgroundTasks,
    ctx: CurrentAuth,
    session: Annotated[AsyncSession, Depends(get_session_with_auth)],
) -> PatientProfileResponse:
    agency_id = _require_agency(ctx)
    result = await patients_service.create_patient(
        session,
        agency_id=agency_id,
        payload=payload,
        admitted_by_user_id=ctx.user_id,
    )
    await session.commit()
    await session.refresh(result.profile)
    # Best-effort audit log.
    with contextlib.suppress(Exception):
        ip, ua = audit_logs_service.request_ip_ua(request)
        await audit_logs_service.audit_log(
            session,
            agency_id=agency_id,
            actor_user_id=ctx.user_id,
            action=AuditAction.CREATE,
            entity_type="PATIENT_PROFILE",
            entity_id=result.profile.id,
            new_data={
                "patient_code": result.profile.patient_code,
                "user_id": str(result.profile.user_id),
            },
            ip_address=ip,
            user_agent=ua,
        )
        await session.commit()
    # Schedule the invitation email after the response is flushed.
    auth_email.send_invitation_email(
        background_tasks,
        to_email=result.email,
        to_name=result.full_name,
        invitation_token=result.invitation_token,
        expires_in_days=settings.INVITATION_TOKEN_EXPIRY_DAYS,
        recipient_user_id=result.user_id,
    )
    return _to_patient_response(result.profile)


@router.get(
    "/patients",
    response_model=dict,
)
async def list_patients_endpoint(
    ctx: CurrentAuth,
    session: Annotated[AsyncSession, Depends(get_session_with_auth)],
    status_filter: UserStatus | None = Query(default=None, alias="status"),
    page: int = Query(default=1, ge=1, le=10000),
    page_size: int = Query(default=20, ge=1, le=100),
) -> dict:
    agency_id = _require_agency(ctx)
    rows, total = await patients_service.list_patients(
        session,
        agency_id=agency_id,
        status_filter=status_filter,
        page=page,
        page_size=page_size,
    )
    data = [PatientProfileSummaryResponse.model_validate(r) for r in rows]
    return build_offset_response(data, total=total, page=page, page_size=page_size)


@router.get(
    "/patients/{patient_id}",
    response_model=PatientProfileResponse,
)
async def get_patient_endpoint(
    patient_id: uuid.UUID,
    ctx: CurrentAuth,
    session: Annotated[AsyncSession, Depends(get_session_with_auth)],
) -> PatientProfileResponse:
    agency_id = _require_agency(ctx)
    patient = await patients_service.get_patient(
        session, patient_id=patient_id, agency_id=agency_id, with_relationships=False
    )
    _ensure_can_view_patient(ctx, patient.user_id)
    return _to_patient_response(patient)


@router.get(
    "/patients/{patient_id}/with-relationships",
    response_model=PatientProfileResponse,
)
async def get_patient_with_relationships_endpoint(
    patient_id: uuid.UUID,
    ctx: CurrentAuth,
    session: Annotated[AsyncSession, Depends(get_session_with_auth)],
) -> PatientProfileResponse:
    agency_id = _require_agency(ctx)
    patient = await patients_service.get_patient(
        session, patient_id=patient_id, agency_id=agency_id, with_relationships=True
    )
    _ensure_can_view_patient(ctx, patient.user_id)
    return _to_patient_response(patient, with_relationships=True)


@router.patch(
    "/patients/{patient_id}",
    response_model=PatientProfileResponse,
    dependencies=[Depends(require_role(UserRole.AGENCY_ADMIN))],
)
async def update_patient_endpoint(
    patient_id: uuid.UUID,
    payload: PatientProfileUpdateRequest,
    request: Request,
    ctx: CurrentAuth,
    session: Annotated[AsyncSession, Depends(get_session_with_auth)],
) -> PatientProfileResponse:
    agency_id = _require_agency(ctx)
    patient = await patients_service.update_patient(
        session, patient_id=patient_id, agency_id=agency_id, payload=payload
    )
    await session.commit()
    await session.refresh(patient)
    # Best-effort audit log.
    try:
        ip, ua = audit_logs_service.request_ip_ua(request)
        await audit_logs_service.audit_log(
            session,
            agency_id=agency_id,
            actor_user_id=ctx.user_id,
            action=AuditAction.UPDATE,
            entity_type="PATIENT_PROFILE",
            entity_id=patient.id,
            new_data=payload.model_dump(mode="json"),
            ip_address=ip,
            user_agent=ua,
        )
        await session.commit()
    except Exception:
        pass
    return _to_patient_response(patient)


@router.delete(
    "/patients/{patient_id}",
    response_model=PatientProfileResponse,
    dependencies=[Depends(require_role(UserRole.AGENCY_ADMIN))],
)
async def archive_patient_endpoint(
    patient_id: uuid.UUID,
    request: Request,
    ctx: CurrentAuth,
    session: Annotated[AsyncSession, Depends(get_session_with_auth)],
) -> PatientProfileResponse:
    agency_id = _require_agency(ctx)
    patient = await patients_service.archive_patient(
        session, patient_id=patient_id, agency_id=agency_id
    )
    await session.commit()
    await session.refresh(patient)
    # Best-effort audit log.
    try:
        ip, ua = audit_logs_service.request_ip_ua(request)
        await audit_logs_service.audit_log(
            session,
            agency_id=agency_id,
            actor_user_id=ctx.user_id,
            action=AuditAction.DELETE,
            entity_type="PATIENT_PROFILE",
            entity_id=patient.id,
            new_data={"status": patient.status.value if hasattr(patient.status, "value") else str(patient.status)},
            ip_address=ip,
            user_agent=ua,
        )
        await session.commit()
    except Exception:
        pass
    return _to_patient_response(patient)


# --------------------------------------------------------------------------
# Patient ↔ Guardian relationships
# --------------------------------------------------------------------------
@router.get(
    "/patients/{patient_id}/guardians",
    response_model=list[PatientGuardianRelationshipResponse],
)
async def list_patient_guardians_endpoint(
    patient_id: uuid.UUID,
    ctx: CurrentAuth,
    session: Annotated[AsyncSession, Depends(get_session_with_auth)],
) -> list[PatientGuardianRelationshipResponse]:
    agency_id = _require_agency(ctx)
    patient = await patients_service.get_patient(
        session, patient_id=patient_id, agency_id=agency_id
    )
    _ensure_can_view_patient(ctx, patient.user_id)
    rows = await patients_service.list_patient_guardians(
        session, patient_id=patient_id, agency_id=agency_id
    )
    return [PatientGuardianRelationshipResponse.model_validate(r) for r in rows]


@router.post(
    "/patients/{patient_id}/guardians",
    response_model=PatientGuardianRelationshipResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_role(UserRole.AGENCY_ADMIN))],
)
async def add_patient_guardian_endpoint(
    patient_id: uuid.UUID,
    payload: PatientGuardianRelationshipCreateRequest,
    request: Request,
    background_tasks: BackgroundTasks,
    ctx: CurrentAuth,
    session: Annotated[AsyncSession, Depends(get_session_with_auth)],
) -> PatientGuardianRelationshipResponse:
    agency_id = _require_agency(ctx)
    result = await patients_service.add_patient_guardian(
        session, patient_id=patient_id, agency_id=agency_id, payload=payload
    )
    await session.commit()
    await session.refresh(result.relationship)
    # Best-effort audit log.
    with contextlib.suppress(Exception):
        ip, ua = audit_logs_service.request_ip_ua(request)
        await audit_logs_service.audit_log(
            session,
            agency_id=agency_id,
            actor_user_id=ctx.user_id,
            action=AuditAction.LINK_PATIENT_GUARDIAN,
            entity_type="PATIENT_GUARDIAN_RELATIONSHIP",
            entity_id=result.relationship.id,
            new_data={
                "patient_id": str(patient_id),
                "guardian_id": str(result.relationship.guardian_id),
                "relationship_type": result.relationship.relationship_type.value
                if hasattr(result.relationship.relationship_type, "value")
                else str(result.relationship.relationship_type),
            },
            ip_address=ip,
            user_agent=ua,
        )
        await session.commit()
    # Only schedule an invitation email when a NEW guardian was
    # created in this call. Linking an existing guardian doesn't
    # generate a fresh invite — the guardian already has a login path.
    if result.new_guardian is not None:
        auth_email.send_invitation_email(
            background_tasks,
            to_email=result.new_guardian.email,
            to_name=result.new_guardian.full_name,
            invitation_token=result.new_guardian.invitation_token,
            expires_in_days=settings.INVITATION_TOKEN_EXPIRY_DAYS,
            recipient_user_id=result.new_guardian.user_id,
        )
    return PatientGuardianRelationshipResponse.model_validate(result.relationship)


# --------------------------------------------------------------------------
# Guardian profiles (standalone)
# --------------------------------------------------------------------------
@router.post(
    "/guardians",
    response_model=GuardianProfileResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_role(UserRole.AGENCY_ADMIN))],
)
async def create_guardian_endpoint(
    payload: GuardianProfileCreateRequest,
    request: Request,
    background_tasks: BackgroundTasks,
    ctx: CurrentAuth,
    session: Annotated[AsyncSession, Depends(get_session_with_auth)],
) -> GuardianProfileResponse:
    agency_id = _require_agency(ctx)
    result = await patients_service.create_guardian(
        session,
        agency_id=agency_id,
        payload=payload,
        invited_by_user_id=ctx.user_id,
    )
    await session.commit()
    await session.refresh(result.profile)
    # Best-effort audit log.
    with contextlib.suppress(Exception):
        ip, ua = audit_logs_service.request_ip_ua(request)
        await audit_logs_service.audit_log(
            session,
            agency_id=agency_id,
            actor_user_id=ctx.user_id,
            action=AuditAction.CREATE,
            entity_type="GUARDIAN_PROFILE",
            entity_id=result.profile.id,
            new_data={"user_id": str(result.profile.user_id)},
            ip_address=ip,
            user_agent=ua,
        )
        await session.commit()
    # Schedule the invitation email after the response is flushed.
    auth_email.send_invitation_email(
        background_tasks,
        to_email=result.email,
        to_name=result.full_name,
        invitation_token=result.invitation_token,
        expires_in_days=settings.INVITATION_TOKEN_EXPIRY_DAYS,
        recipient_user_id=result.user_id,
    )
    return GuardianProfileResponse.model_validate(result.profile)


@router.get(
    "/guardians",
    response_model=dict,
)
async def list_guardians_endpoint(
    ctx: CurrentAuth,
    session: Annotated[AsyncSession, Depends(get_session_with_auth)],
    page: int = Query(default=1, ge=1, le=10000),
    page_size: int = Query(default=20, ge=1, le=100),
) -> dict:
    agency_id = _require_agency(ctx)
    rows, total = await patients_service.list_guardians(
        session, agency_id=agency_id, page=page, page_size=page_size
    )
    data = [GuardianProfileResponse.model_validate(r) for r in rows]
    return build_offset_response(data, total=total, page=page, page_size=page_size)


@router.get(
    "/guardians/{guardian_id}",
    response_model=GuardianProfileResponse,
)
async def get_guardian_endpoint(
    guardian_id: uuid.UUID,
    ctx: CurrentAuth,
    session: Annotated[AsyncSession, Depends(get_session_with_auth)],
) -> GuardianProfileResponse:
    agency_id = _require_agency(ctx)
    guardian = await patients_service.get_guardian(
        session, guardian_id=guardian_id, agency_id=agency_id
    )
    _ensure_can_view_guardian(ctx, guardian.user_id)
    return GuardianProfileResponse.model_validate(guardian)


@router.patch(
    "/guardians/{guardian_id}",
    response_model=GuardianProfileResponse,
    dependencies=[Depends(require_role(UserRole.AGENCY_ADMIN))],
)
async def update_guardian_endpoint(
    guardian_id: uuid.UUID,
    payload: GuardianProfileUpdateRequest,
    request: Request,
    ctx: CurrentAuth,
    session: Annotated[AsyncSession, Depends(get_session_with_auth)],
) -> GuardianProfileResponse:
    agency_id = _require_agency(ctx)
    guardian = await patients_service.update_guardian(
        session, guardian_id=guardian_id, agency_id=agency_id, payload=payload
    )
    await session.commit()
    await session.refresh(guardian)
    # Best-effort audit log.
    try:
        ip, ua = audit_logs_service.request_ip_ua(request)
        await audit_logs_service.audit_log(
            session,
            agency_id=agency_id,
            actor_user_id=ctx.user_id,
            action=AuditAction.UPDATE,
            entity_type="GUARDIAN_PROFILE",
            entity_id=guardian.id,
            new_data=payload.model_dump(mode="json"),
            ip_address=ip,
            user_agent=ua,
        )
        await session.commit()
    except Exception:
        pass
    return GuardianProfileResponse.model_validate(guardian)


@router.delete(
    "/guardians/{guardian_id}",
    response_model=GuardianProfileResponse,
    dependencies=[Depends(require_role(UserRole.AGENCY_ADMIN))],
)
async def archive_guardian_endpoint(
    guardian_id: uuid.UUID,
    request: Request,
    ctx: CurrentAuth,
    session: Annotated[AsyncSession, Depends(get_session_with_auth)],
) -> GuardianProfileResponse:
    agency_id = _require_agency(ctx)
    guardian = await patients_service.archive_guardian(
        session, guardian_id=guardian_id, agency_id=agency_id
    )
    await session.commit()
    await session.refresh(guardian)
    # Best-effort audit log.
    try:
        ip, ua = audit_logs_service.request_ip_ua(request)
        await audit_logs_service.audit_log(
            session,
            agency_id=agency_id,
            actor_user_id=ctx.user_id,
            action=AuditAction.DELETE,
            entity_type="GUARDIAN_PROFILE",
            entity_id=guardian.id,
            new_data={"status": guardian.status.value if hasattr(guardian.status, "value") else str(guardian.status)},
            ip_address=ip,
            user_agent=ua,
        )
        await session.commit()
    except Exception:
        pass
    return GuardianProfileResponse.model_validate(guardian)


# --------------------------------------------------------------------------
# Patient ↔ Guardian relationship (edit/delete)
# --------------------------------------------------------------------------
@router.patch(
    "/patient-guardian-relationships/{relationship_id}",
    response_model=PatientGuardianRelationshipResponse,
    dependencies=[Depends(require_role(UserRole.AGENCY_ADMIN))],
)
async def update_patient_guardian_endpoint(
    relationship_id: uuid.UUID,
    payload: PatientGuardianRelationshipUpdateRequest,
    request: Request,
    ctx: CurrentAuth,
    session: Annotated[AsyncSession, Depends(get_session_with_auth)],
) -> PatientGuardianRelationshipResponse:
    agency_id = _require_agency(ctx)
    rel = await patients_service.update_patient_guardian(
        session,
        relationship_id=relationship_id,
        agency_id=agency_id,
        payload=payload,
    )
    await session.commit()
    await session.refresh(rel)
    # Best-effort audit log.
    try:
        ip, ua = audit_logs_service.request_ip_ua(request)
        await audit_logs_service.audit_log(
            session,
            agency_id=agency_id,
            actor_user_id=ctx.user_id,
            action=AuditAction.UPDATE,
            entity_type="PATIENT_GUARDIAN_RELATIONSHIP",
            entity_id=rel.id,
            new_data=payload.model_dump(mode="json"),
            ip_address=ip,
            user_agent=ua,
        )
        await session.commit()
    except Exception:
        pass
    return PatientGuardianRelationshipResponse.model_validate(rel)


@router.delete(
    "/patient-guardian-relationships/{relationship_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_role(UserRole.AGENCY_ADMIN))],
)
async def delete_patient_guardian_endpoint(
    relationship_id: uuid.UUID,
    request: Request,
    ctx: CurrentAuth,
    session: Annotated[AsyncSession, Depends(get_session_with_auth)],
) -> Response:
    agency_id = _require_agency(ctx)
    await patients_service.delete_patient_guardian(
        session, relationship_id=relationship_id, agency_id=agency_id
    )
    await session.commit()
    # Best-effort audit log.
    try:
        ip, ua = audit_logs_service.request_ip_ua(request)
        await audit_logs_service.audit_log(
            session,
            agency_id=agency_id,
            actor_user_id=ctx.user_id,
            action=AuditAction.UNLINK_PATIENT_GUARDIAN,
            entity_type="PATIENT_GUARDIAN_RELATIONSHIP",
            entity_id=relationship_id,
            new_data={},
            ip_address=ip,
            user_agent=ua,
        )
        await session.commit()
    except Exception:
        pass
    return Response(status_code=status.HTTP_204_NO_CONTENT)


__all__ = ["router"]
