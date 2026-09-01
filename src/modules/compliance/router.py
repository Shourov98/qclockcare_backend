"""Compliance router — `/admin/compliance` documents + licenses + issues.

Endpoints:
  GET    /admin/compliance/stats
  GET    /admin/compliance/documents
  GET    /admin/compliance/documents/missing
  POST   /admin/compliance/documents
  PATCH  /admin/compliance/documents/{id}
  DELETE /admin/compliance/documents/{id}
  GET    /admin/compliance/licenses
  POST   /admin/compliance/licenses
  PATCH  /admin/compliance/licenses/{id}
  DELETE /admin/compliance/licenses/{id}

  GET    /admin/compliance/issues
  GET    /admin/compliance/issues/stats
  POST   /admin/compliance/issues
  GET    /admin/compliance/issues/{id}
  PATCH  /admin/compliance/issues/{id}
  POST   /admin/compliance/issues/{id}/resolve
  POST   /admin/compliance/issues/{id}/dismiss
  POST   /admin/compliance/issues/{id}/assign
  DELETE /admin/compliance/issues/{id}

Auth: SUPER_ADMIN (full access) OR PLATFORM_ADMIN with AGENCIES scope.
The compliance admin surfaces are the natural pair to AGENCIES scope —
admins who can edit agencies can also track their documents/licenses.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from src.modules.audit_logs import service as audit_logs_service
from src.modules.compliance import issues as issues_module
from src.modules.compliance import service as compliance_service
from src.modules.compliance.schemas import (
    AgencyDocumentCreateRequest,
    AgencyDocumentResponse,
    AgencyDocumentUpdateRequest,
    AgencyLicenseCreateRequest,
    AgencyLicenseResponse,
    AgencyLicenseUpdateRequest,
    ComplianceStatsResponse,
    DocumentListResponse,
    LicenseListResponse,
    OffsetPaginationMeta,
)
from src.modules.identity.dependencies import (
    CurrentAuth,
    get_session_with_auth,
)
from src.modules.identity.scope_deps import require_scope
from src.shared.domain.enums import (
    AdminScope,
    AuditAction,
    ComplianceIssueCategory,
    ComplianceIssueSeverity,
    ComplianceIssueStatus,
    DocumentStatus,
    DocumentType,
    LicenseStatus,
)
from src.shared.schemas.docs import standard_responses

router = APIRouter(prefix="/admin/compliance", tags=["admin-compliance"])

# SUPER_ADMIN OR PLATFORM_ADMIN with AGENCIES scope.
_COMPLIANCE_AUTH = [Depends(require_scope(AdminScope.AGENCIES))]


@router.get(
    "/stats",
    response_model=ComplianceStatsResponse,
    dependencies=_COMPLIANCE_AUTH,
    responses=standard_responses(include=[401, 403]),
    summary="Compliance summary counts",
)
async def get_compliance_stats_endpoint(
    session: Annotated[AsyncSession, Depends(get_session_with_auth)],
) -> ComplianceStatsResponse:
    return ComplianceStatsResponse.model_validate(
        await compliance_service.get_compliance_stats(session)
    )


# --------------------------------------------------------------------------
# Documents
# --------------------------------------------------------------------------
@router.get(
    "/documents",
    response_model=DocumentListResponse,
    dependencies=_COMPLIANCE_AUTH,
    responses=standard_responses(include=[401, 403]),
    summary="List per-agency required documents",
)
async def list_documents_endpoint(
    session: Annotated[AsyncSession, Depends(get_session_with_auth)],
    page: Annotated[int, Query(ge=1, le=10000)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
    agency_id: Annotated[uuid.UUID | None, Query()] = None,
    status_filter: Annotated[DocumentStatus | None, Query(alias="status")] = None,
    search: Annotated[str | None, Query(max_length=255)] = None,
    include_deleted: Annotated[bool, Query()] = False,
) -> DocumentListResponse:
    items, total = await compliance_service.list_documents(
        session,
        page=page,
        page_size=page_size,
        agency_id=agency_id,
        status_filter=status_filter,
        search=search,
        include_deleted=include_deleted,
    )
    total_pages = (total + page_size - 1) // page_size if total else 0
    return DocumentListResponse(
        data=[AgencyDocumentResponse.model_validate(i) for i in items],
        pagination=OffsetPaginationMeta(
            page=page, page_size=page_size, total=total, total_pages=total_pages
        ),
    )


@router.get(
    "/documents/missing",
    response_model=DocumentListResponse,
    dependencies=_COMPLIANCE_AUTH,
    responses=standard_responses(include=[401, 403]),
    summary="Per-agency missing-documents report",
    description=(
        "Returns one row per agency that has ≥1 document in MISSING status, "
        "with up to 50 of their missing documents embedded inline."
    ),
)
async def list_missing_documents_endpoint(
    session: Annotated[AsyncSession, Depends(get_session_with_auth)],
    page: Annotated[int, Query(ge=1, le=10000)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
    search: Annotated[str | None, Query(max_length=255)] = None,
) -> DocumentListResponse:
    """The Missing-documents report uses a different shape (per-agency) than
    the regular documents list. We return the documents inside each row,
    but the wrapper stays `DocumentListResponse` for consistency with the
    FE's pagination helper. Use `?flat=true` to fall back to the simple
    documents list."""
    items, total = await compliance_service.list_missing_documents(
        session,
        page=page,
        page_size=page_size,
        search=search,
    )
    total_pages = (total + page_size - 1) // page_size if total else 0
    # Flatten the rows so DocumentListResponse (which is a list of
    # AgencyDocumentResponse) works. The agency grouping info is
    # recoverable on the FE via the embedded `agency_id` field.
    flat_docs = [d for row in items for d in row["documents"]]
    return DocumentListResponse(
        data=[AgencyDocumentResponse.model_validate(d) for d in flat_docs],
        pagination=OffsetPaginationMeta(
            page=page, page_size=page_size, total=total, total_pages=total_pages
        ),
    )


@router.post(
    "/documents",
    response_model=AgencyDocumentResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=_COMPLIANCE_AUTH,
    responses=standard_responses(include=[401, 403, 404, 422]),
    summary="Create a required-document record",
)
async def create_document_endpoint(
    payload: AgencyDocumentCreateRequest,
    session: Annotated[AsyncSession, Depends(get_session_with_auth)],
) -> AgencyDocumentResponse:
    doc = await compliance_service.create_document(
        session,
        agency_id=payload.agency_id,
        name=payload.name,
        doc_type=payload.doc_type,
        status=payload.status,
        description=payload.description,
        expires_at=payload.expires_at,
        file_url=payload.file_url,
    )
    return AgencyDocumentResponse.model_validate(doc)


@router.patch(
    "/documents/{document_id}",
    response_model=AgencyDocumentResponse,
    dependencies=_COMPLIANCE_AUTH,
    responses=standard_responses(include=[401, 403, 404, 422]),
    summary="Update a document record",
)
async def update_document_endpoint(
    document_id: uuid.UUID,
    payload: AgencyDocumentUpdateRequest,
    session: Annotated[AsyncSession, Depends(get_session_with_auth)],
) -> AgencyDocumentResponse:
    changes = payload.model_dump(exclude_unset=True)
    doc = await compliance_service.update_document(
        session, document_id=document_id, changes=changes
    )
    return AgencyDocumentResponse.model_validate(doc)


@router.delete(
    "/documents/{document_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=_COMPLIANCE_AUTH,
    responses=standard_responses(include=[401, 403, 404]),
    summary="Soft-delete a document record",
)
async def delete_document_endpoint(
    document_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session_with_auth)],
) -> None:
    await compliance_service.soft_delete_document(
        session, document_id=document_id
    )


# --------------------------------------------------------------------------
# Licenses
# --------------------------------------------------------------------------
@router.get(
    "/licenses",
    response_model=LicenseListResponse,
    dependencies=_COMPLIANCE_AUTH,
    responses=standard_responses(include=[401, 403]),
    summary="List expiring licenses",
)
async def list_licenses_endpoint(
    session: Annotated[AsyncSession, Depends(get_session_with_auth)],
    page: Annotated[int, Query(ge=1, le=10000)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
    agency_id: Annotated[uuid.UUID | None, Query()] = None,
    status_filter: Annotated[LicenseStatus | None, Query(alias="status")] = None,
    search: Annotated[str | None, Query(max_length=255)] = None,
    expiring_within_days: Annotated[int | None, Query(ge=1, le=365)] = None,
    include_deleted: Annotated[bool, Query()] = False,
) -> LicenseListResponse:
    items, total = await compliance_service.list_licenses(
        session,
        page=page,
        page_size=page_size,
        agency_id=agency_id,
        status_filter=status_filter,
        search=search,
        expiring_within_days=expiring_within_days,
        include_deleted=include_deleted,
    )
    total_pages = (total + page_size - 1) // page_size if total else 0
    return LicenseListResponse(
        data=[AgencyLicenseResponse.model_validate(i) for i in items],
        pagination=OffsetPaginationMeta(
            page=page, page_size=page_size, total=total, total_pages=total_pages
        ),
    )


@router.post(
    "/licenses",
    response_model=AgencyLicenseResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=_COMPLIANCE_AUTH,
    responses=standard_responses(include=[401, 403, 404, 422]),
    summary="Create a license record",
)
async def create_license_endpoint(
    payload: AgencyLicenseCreateRequest,
    session: Annotated[AsyncSession, Depends(get_session_with_auth)],
) -> AgencyLicenseResponse:
    lic = await compliance_service.create_license(
        session,
        agency_id=payload.agency_id,
        name=payload.name,
        expires_at=payload.expires_at,
        doc_type=payload.doc_type,
        status=payload.status,
        issued_at=payload.issued_at,
        reference_number=payload.reference_number,
        notes=payload.notes,
    )
    return AgencyLicenseResponse.model_validate(lic)


@router.patch(
    "/licenses/{license_id}",
    response_model=AgencyLicenseResponse,
    dependencies=_COMPLIANCE_AUTH,
    responses=standard_responses(include=[401, 403, 404, 422]),
    summary="Update a license record",
)
async def update_license_endpoint(
    license_id: uuid.UUID,
    payload: AgencyLicenseUpdateRequest,
    session: Annotated[AsyncSession, Depends(get_session_with_auth)],
) -> AgencyLicenseResponse:
    changes = payload.model_dump(exclude_unset=True)
    lic = await compliance_service.update_license(
        session, license_id=license_id, changes=changes
    )
    return AgencyLicenseResponse.model_validate(lic)


@router.delete(
    "/licenses/{license_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=_COMPLIANCE_AUTH,
    responses=standard_responses(include=[401, 403, 404]),
    summary="Soft-delete a license record",
)
async def delete_license_endpoint(
    license_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session_with_auth)],
) -> None:
    await compliance_service.soft_delete_license(
        session, license_id=license_id
    )


# --------------------------------------------------------------------------
# Compliance issue queue (admin /admin/compliance/issues/*)
# --------------------------------------------------------------------------
def _audit_compliance_issue(
    *,
    session: AsyncSession,
    request: Request,
    auth: CurrentAuth,
    issue_id: uuid.UUID,
    agency_id: uuid.UUID,
    action: AuditAction,
    new_data: dict | None = None,
    old_data: dict | None = None,
) -> None:
    """Best-effort audit row for a compliance-issue mutation.

    Mirrors the convention used by the visits/billing/notifications
    routers — wrapping in `try/except Exception: pass` so an audit
    failure never breaks the originating write.
    """
    try:
        ip, ua = audit_logs_service.request_ip_ua(request)
        audit_logs_service.audit_log(
            session,
            agency_id=agency_id,
            actor_user_id=auth.user_id,
            action=action,
            entity_type="COMPLIANCE_ISSUE",
            entity_id=issue_id,
            new_data=new_data,
            old_data=old_data,
            ip_address=ip,
            user_agent=ua,
        )
        session.commit()
    except Exception:
        pass


@router.get(
    "/issues/stats",
    response_model=issues_module.ComplianceIssueStatsResponse,
    dependencies=_COMPLIANCE_AUTH,
    responses=standard_responses(include=[401, 403]),
    summary="Compliance issue queue dashboard counts",
)
async def get_compliance_issue_stats_endpoint(
    session: Annotated[AsyncSession, Depends(get_session_with_auth)],
    agency_id: Annotated[uuid.UUID | None, Query()] = None,
) -> issues_module.ComplianceIssueStatsResponse:
    """Aggregate counts for the issue-queue dashboard header."""
    counts = await issues_module.get_compliance_issue_stats(
        session, agency_id=agency_id
    )
    return issues_module.ComplianceIssueStatsResponse(**counts)


@router.get(
    "/issues",
    response_model=issues_module.ComplianceIssueListResponse,
    dependencies=_COMPLIANCE_AUTH,
    responses=standard_responses(include=[401, 403]),
    summary="List compliance issues",
)
async def list_compliance_issues_endpoint(
    session: Annotated[AsyncSession, Depends(get_session_with_auth)],
    page: Annotated[int, Query(ge=1, le=10000)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
    agency_id: Annotated[uuid.UUID | None, Query()] = None,
    severity: Annotated[ComplianceIssueSeverity | None, Query()] = None,
    status_filter: Annotated[ComplianceIssueStatus | None, Query(alias="status")] = None,
    category: Annotated[ComplianceIssueCategory | None, Query()] = None,
    assignee_user_id: Annotated[uuid.UUID | None, Query()] = None,
    search: Annotated[str | None, Query(max_length=255)] = None,
    include_resolved: Annotated[bool, Query()] = True,
) -> issues_module.ComplianceIssueListResponse:
    items, total = await issues_module.list_compliance_issues(
        session,
        page=page,
        page_size=page_size,
        agency_id=agency_id,
        severity=severity,
        status_filter=status_filter,
        category=category,
        assignee_user_id=assignee_user_id,
        search=search,
        include_resolved=include_resolved,
    )
    return issues_module.ComplianceIssueListResponse(
        data=[
            issues_module.ComplianceIssueResponse.model_validate(i)
            for i in items
        ],
        pagination=issues_module.build_issue_pagination(
            page=page, page_size=page_size, total=total
        ),
    )


@router.post(
    "/issues",
    response_model=issues_module.ComplianceIssueResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=_COMPLIANCE_AUTH,
    responses=standard_responses(include=[401, 403, 422]),
    summary="Create a compliance issue",
)
async def create_compliance_issue_endpoint(
    payload: issues_module.ComplianceIssueCreateRequest,
    request: Request,
    auth: CurrentAuth,
    session: Annotated[AsyncSession, Depends(get_session_with_auth)],
) -> issues_module.ComplianceIssueResponse:
    row = await issues_module.create_compliance_issue(
        session,
        agency_id=payload.agency_id,
        title=payload.title,
        severity=payload.severity,
        category=payload.category,
        description=payload.description,
        reporter_user_id=auth.user_id,
        assignee_user_id=payload.assignee_user_id,
        due_at=payload.due_at,
        linked_entity_type=payload.linked_entity_type,
        linked_entity_id=payload.linked_entity_id,
    )
    _audit_compliance_issue(
        session=session,
        request=request,
        auth=auth,
        issue_id=row.id,
        agency_id=row.agency_id,
        action=AuditAction.COMPLIANCE_ISSUE_CREATED,
        new_data={
            "title": row.title,
            "severity": row.severity.value,
            "category": row.category.value,
            "status": row.status.value,
            "assignee_user_id": (
                str(row.assignee_user_id) if row.assignee_user_id else None
            ),
            "due_at": row.due_at.isoformat() if row.due_at else None,
        },
    )
    return issues_module.ComplianceIssueResponse.model_validate(row)


@router.get(
    "/issues/{issue_id}",
    response_model=issues_module.ComplianceIssueResponse,
    dependencies=_COMPLIANCE_AUTH,
    responses=standard_responses(include=[401, 403, 404]),
    summary="Fetch one compliance issue",
)
async def get_compliance_issue_endpoint(
    issue_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session_with_auth)],
) -> issues_module.ComplianceIssueResponse:
    row = await issues_module.get_compliance_issue(
        session, issue_id=issue_id
    )
    return issues_module.ComplianceIssueResponse.model_validate(row)


@router.patch(
    "/issues/{issue_id}",
    response_model=issues_module.ComplianceIssueResponse,
    dependencies=_COMPLIANCE_AUTH,
    responses=standard_responses(include=[401, 403, 404, 422]),
    summary="Update a compliance issue",
)
async def update_compliance_issue_endpoint(
    issue_id: uuid.UUID,
    payload: issues_module.ComplianceIssueUpdateRequest,
    request: Request,
    auth: CurrentAuth,
    session: Annotated[AsyncSession, Depends(get_session_with_auth)],
) -> issues_module.ComplianceIssueResponse:
    changes = payload.model_dump(exclude_unset=True)
    row = await issues_module.get_compliance_issue(
        session, issue_id=issue_id, include_deleted=True
    )
    old_data = {
        "title": row.title,
        "severity": row.severity.value,
        "category": row.category.value,
        "status": row.status.value,
        "assignee_user_id": (
            str(row.assignee_user_id) if row.assignee_user_id else None
        ),
        "due_at": row.due_at.isoformat() if row.due_at else None,
    }
    updated = await issues_module.update_compliance_issue(
        session, issue_id=issue_id, changes=changes
    )
    _audit_compliance_issue(
        session=session,
        request=request,
        auth=auth,
        issue_id=updated.id,
        agency_id=updated.agency_id,
        action=AuditAction.COMPLIANCE_ISSUE_UPDATED,
        old_data=old_data,
        new_data=changes,
    )
    return issues_module.ComplianceIssueResponse.model_validate(updated)


@router.post(
    "/issues/{issue_id}/resolve",
    response_model=issues_module.ComplianceIssueResponse,
    dependencies=_COMPLIANCE_AUTH,
    responses=standard_responses(include=[401, 403, 404, 422]),
    summary="Mark a compliance issue resolved",
)
async def resolve_compliance_issue_endpoint(
    issue_id: uuid.UUID,
    request: Request,
    payload: issues_module.ComplianceIssueResolveRequest,
    auth: CurrentAuth,
    session: Annotated[AsyncSession, Depends(get_session_with_auth)],
) -> issues_module.ComplianceIssueResponse:
    row = await issues_module.resolve_compliance_issue(
        session,
        issue_id=issue_id,
        resolved_by_user_id=auth.user_id,
    )
    _audit_compliance_issue(
        session=session,
        request=request,
        auth=auth,
        issue_id=row.id,
        agency_id=row.agency_id,
        action=AuditAction.COMPLIANCE_ISSUE_RESOLVED,
        new_data={
            "resolution_note": payload.resolution_note,
            "resolved_at": (
                row.resolved_at.isoformat() if row.resolved_at else None
            ),
        },
    )
    return issues_module.ComplianceIssueResponse.model_validate(row)


@router.post(
    "/issues/{issue_id}/dismiss",
    response_model=issues_module.ComplianceIssueResponse,
    dependencies=_COMPLIANCE_AUTH,
    responses=standard_responses(include=[401, 403, 404, 422]),
    summary="Dismiss a compliance issue without action",
)
async def dismiss_compliance_issue_endpoint(
    issue_id: uuid.UUID,
    request: Request,
    payload: issues_module.ComplianceIssueDismissRequest,
    auth: CurrentAuth,
    session: Annotated[AsyncSession, Depends(get_session_with_auth)],
) -> issues_module.ComplianceIssueResponse:
    row = await issues_module.dismiss_compliance_issue(
        session,
        issue_id=issue_id,
        resolved_by_user_id=auth.user_id,
    )
    _audit_compliance_issue(
        session=session,
        request=request,
        auth=auth,
        issue_id=row.id,
        agency_id=row.agency_id,
        action=AuditAction.COMPLIANCE_ISSUE_DISMISSED,
        new_data={
            "reason": payload.reason,
            "resolved_at": (
                row.resolved_at.isoformat() if row.resolved_at else None
            ),
        },
    )
    return issues_module.ComplianceIssueResponse.model_validate(row)


@router.post(
    "/issues/{issue_id}/assign",
    response_model=issues_module.ComplianceIssueResponse,
    dependencies=_COMPLIANCE_AUTH,
    responses=standard_responses(include=[401, 403, 404, 422]),
    summary="Assign a compliance issue to a user",
)
async def assign_compliance_issue_endpoint(
    issue_id: uuid.UUID,
    request: Request,
    payload: issues_module.ComplianceIssueAssignRequest,
    auth: CurrentAuth,
    session: Annotated[AsyncSession, Depends(get_session_with_auth)],
) -> issues_module.ComplianceIssueResponse:
    row = await issues_module.assign_compliance_issue(
        session,
        issue_id=issue_id,
        assignee_user_id=payload.assignee_user_id,
    )
    _audit_compliance_issue(
        session=session,
        request=request,
        auth=auth,
        issue_id=row.id,
        agency_id=row.agency_id,
        action=AuditAction.COMPLIANCE_ISSUE_UPDATED,
        new_data={
            "event": "assign",
            "assignee_user_id": str(payload.assignee_user_id),
            "status": row.status.value,
        },
    )
    return issues_module.ComplianceIssueResponse.model_validate(row)


@router.delete(
    "/issues/{issue_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=_COMPLIANCE_AUTH,
    responses=standard_responses(include=[401, 403, 404]),
    summary="Soft-delete a compliance issue",
)
async def delete_compliance_issue_endpoint(
    issue_id: uuid.UUID,
    request: Request,
    auth: CurrentAuth,
    session: Annotated[AsyncSession, Depends(get_session_with_auth)],
) -> None:
    row = await issues_module.get_compliance_issue(
        session, issue_id=issue_id, include_deleted=True
    )
    await issues_module.soft_delete_compliance_issue(
        session, issue_id=issue_id
    )
    _audit_compliance_issue(
        session=session,
        request=request,
        auth=auth,
        issue_id=row.id,
        agency_id=row.agency_id,
        action=AuditAction.DELETE,
        entity_type="COMPLIANCE_ISSUE",
        old_data={"deleted": True},
    )


__all__ = ["router"]