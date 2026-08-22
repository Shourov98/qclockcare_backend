"""Tickets module — internal admin support tickets.

Routes (mounted at `/admin/tickets`):
  GET    /admin/tickets                  — list with filters
  POST   /admin/tickets                  — create
  GET    /admin/tickets/stats            — counts by status for the dashboard cards
  GET    /admin/tickets/{id}             — fetch one with comments
  PATCH  /admin/tickets/{id}             — update fields (status, priority, assignee, …)
  DELETE /admin/tickets/{id}             — soft delete
  POST   /admin/tickets/{id}/comments    — add a ticket comment

Tickets are scoped to the platform admin dashboard (SUPER_ADMIN or
PLATFORM_ADMIN with SUPPORT scope). They are NOT tenant-scoped:
`agency_id` is nullable so cross-tenant issues (e.g. "Stripe webhooks
are dropping globally") can be tracked.
"""