"""Reports module — Claude-powered narrative reports + PDF/CSV/Excel export.

This module exposes the `/reports/{type}/stream` SSE endpoint that
aggregates tenant-scoped data from the visits / staff / patients /
audit_logs / appointments modules, sends the aggregate as a prompt to
Claude, and streams the model's narrative back to the SPA token-by-token.
Each run is persisted to `report_runs` so the result can be re-read,
exported to PDF / CSV / XLSX, or listed in the report-history sidebar.

The 9 report types match the cards on the agency's Reports dashboard
(`farhan-salad-website/app/(dashboard)/reports/page.tsx`):

  - VISIT_SUMMARY      — date range, all visits, EVV status, hours billed
  - BILLING            — claims submitted, paid, denied by period
  - COMPLIANCE         — credential status, expiring docs, audit readiness
  - CLIENT             — SA utilization, visit frequency per client
  - STAFF              — hours worked, visits per caregiver, credentials
  - EVV                — GPS verification, missed clock-ins, manual overrides
  - GROUP_HOME         — ISP compliance, incident log, medication adherence
  - AUDIT_READINESS    — DHS / Optum preparation summary
  - CUSTOM             — build your own with filters
  - AI_INSIGHTS        — cross-cutting summary, drives the AI Insights banner

A few of these have no source tables yet (BILLING claims/denials,
GROUP_HOME / ISP / incidents / medications, Service Authorizations).
Their aggregators return `{"_data_availability": "limited"}` and the
prompts explicitly tell Claude not to invent numbers from thin air.
Building the missing tables is out of scope for this module — see the
plan file at `/home/shourov/.puku-cli/plans/inherited-coalescing-naur.md`.
"""

from __future__ import annotations

from src.modules.reports.router import router

__all__ = ["router"]
