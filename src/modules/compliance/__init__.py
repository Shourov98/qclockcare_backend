"""Compliance module — agency documents and expiring licenses.

Endpoints (mounted under `/admin/compliance`):

  GET    /admin/compliance/stats                  — dashboard counts
  GET    /admin/compliance/documents              — list per-agency required docs
  GET    /admin/compliance/documents/missing      — agencies with missing docs
  POST   /admin/compliance/documents              — record a new required doc
  PATCH  /admin/compliance/documents/{id}         — update doc fields
  DELETE /admin/compliance/documents/{id}         — archive a doc record
  GET    /admin/compliance/licenses               — list expiring licenses
  POST   /admin/compliance/licenses               — add an expiring license
  PATCH  /admin/compliance/licenses/{id}          — update license
  DELETE /admin/compliance/licenses/{id}          — archive a license

Auth: SUPER_ADMIN (full access) OR PLATFORM_ADMIN with AGENCIES scope.
This is the natural pair to AGENCIES scope — the documents/licenses
admin surfaces are scoped per-agency.
"""
