"""Patient/Guardian ↔ AGENCY_ADMIN help & support tickets.

Distinct from `src/modules/tickets/` (which is the internal admin
helpdesk under `/admin/tickets`). This module exposes the public
support surface — what the patient/guardian app's "Need help?" button
hits and what the agency-admin dashboard's "Support inbox" reads
from.

Two URL groups:
  - `/portal/support/tickets/...` — PATIENT / GUARDIAN self-serve.
  - `/agency/support/tickets/...` — AGENCY_ADMIN inbox + replies.

Both URL groups share one underlying table (`support_tickets`) and one
messages table (`support_ticket_messages`); RLS + the service-layer
auth helper decide which rows each caller can see.
"""