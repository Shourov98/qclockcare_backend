"""Demo seed for the admin dashboard pages.

Wipes & re-seeds rows in the four tables that power
`/admin/tickets`, `/admin/compliance/documents`, and
`/admin/compliance/licenses`:

    - tickets
    - ticket_comments
    - agency_documents
    - agency_licenses

Nothing else is touched. The script is idempotent in the destructive
sense: every run produces the same dataset, so re-running is safe and
gives you a clean demo state.

Designed to run against either the local Postgres or the Supabase
Direct connection. Reads `settings.effective_database_url` like
`seed_test_user.py` does, so you can switch by overriding
`DATABASE_URL` in the environment.

Run:

    uv run python scripts/seed_admin_dashboard_data.py

What gets created
-----------------

Tickets (8 total):

    TK-0001  CRITICAL  OPEN          Stripe webhook processing failing globally
    TK-0002  HIGH      IN_PROGRESS   Agency 'Atomic Test Agency A' can't reset password
    TK-0003  MEDIUM    PENDING       Need clarification on audit log retention policy
    TK-0004  LOW       RESOLVED      Misleading tooltip on Agencies > Plan column
    TK-0005  MEDIUM    CLOSED        Add CSV export to /admin/audit-logs
    TK-0006  HIGH      OPEN          Compliance page: bulk-upload documents UX
    TK-0007  CRITICAL  IN_PROGRESS   Production database CPU spiked at 03:12 UTC
    TK-0008  LOW       PENDING       Cosmetic: rename 'PLATFORM_ADMIN' label

Each ticket gets 2–4 comments spanning COMMENT / STATUS_CHANGE /
ASSIGNMENT kinds so the timeline view renders meaningfully.

Agency documents (12 total across 4 agencies):

    3 LICENSE rows per agency × 4 agencies. Mix of:
      - 4 VALID   (>90 days out)
      - 3 EXPIRING (7–30 days)
      - 2 EXPIRED (in the past)
      - 3 MISSING (no file_url, no expires_at)

Agency licenses (10 total across 4 agencies):

    Mix of:
      - 4 VALID
      - 2 WARNING
      - 2 CRITICAL
      - 1 UPCOMING
      - 1 EXPIRED

All reporter / author / assignee FKs point at the seeded super admin
(`super@qlockcare.dev`) so the script has zero dependencies on any
other seed. If that user doesn't exist the script prints a clear
error and exits — run `seed_test_user.py` first.
"""

from __future__ import annotations

import asyncio
import json
import sys
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from src.core.config import settings


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
SUPER_ADMIN_EMAIL = "super@qlockcare.dev"


@dataclass(frozen=True)
class SeedResult:
    tickets: int
    ticket_comments: int
    documents: int
    licenses: int


async def _require_super_admin_id(engine: AsyncEngine) -> uuid.UUID:
    """Look up the seeded super admin. Bail with a clear message if missing."""
    async with engine.connect() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT u.id FROM users u "
                    "JOIN user_roles ur ON ur.user_id = u.id "
                    "WHERE u.email = :e AND ur.role = 'SUPER_ADMIN' LIMIT 1"
                ),
                {"e": SUPER_ADMIN_EMAIL},
            )
        ).first()
    if row is None:
        raise SystemExit(
            f"ERROR: super admin {SUPER_ADMIN_EMAIL!r} not found.\n"
            "Run scripts/seed_test_user.py first — the dashboard seed "
            "depends on that user for FK references."
        )
    return row[0]


async def _pick_agencies(engine: AsyncEngine, n: int) -> list[tuple[uuid.UUID, str]]:
    """Return up to `n` ACTIVE agencies (id, name)."""
    async with engine.connect() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT id, name FROM agencies "
                    "WHERE status = 'ACTIVE' AND deleted_at IS NULL "
                    "ORDER BY created_at LIMIT :n"
                ),
                {"n": n},
            )
        ).all()
    return [(r[0], r[1]) for r in rows]


async def _wipe(engine: AsyncEngine) -> None:
    """Clear rows in dependency order. CASCADE handles ticket_comments."""
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM ticket_comments"))
        await conn.execute(text("DELETE FROM tickets"))
        await conn.execute(text("DELETE FROM agency_documents"))
        await conn.execute(text("DELETE FROM agency_licenses"))
        await conn.execute(
            text(
                # Reset the per-day ticket-code counter so the demo
                # tickets get TK-0001..TK-0008 deterministically.
                "DELETE FROM ticket_code_sequence"
            )
        )


def _now() -> datetime:
    return datetime.now(tz=UTC)


async def _insert_tickets(
    engine: AsyncEngine, reporter_id: uuid.UUID
) -> tuple[int, int]:
    """Insert 8 tickets + a comment thread for each. Return (tickets, comments)."""
    now = _now()

    # (code, title, description, status, priority, agency_idx, hours_ago)
    ticket_specs: list[tuple[str, str, str, str, str, int | None, int]] = [
        (
            "TK-0001",
            "Stripe webhook processing failing globally",
            "Multiple agencies report failed payments since 02:30 UTC. Webhook "
            "endpoint returning 500 on every event. Investigating idempotency.",
            "OPEN",
            "CRITICAL",
            None,
            1,
        ),
        (
            "TK-0002",
            "Agency 'Atomic Test Agency A' can't reset password",
            "Agency admin reports the forgot-password email is never received. "
            "Checked SES logs — message accepted but bounced on recipient side.",
            "IN_PROGRESS",
            "HIGH",
            2,  # Atomic Test Agency A — first match in our pick
            6,
        ),
        (
            "TK-0003",
            "Need clarification on audit log retention policy",
            "Compliance team is asking how long we keep audit_logs before "
            "archival. Need a written answer from legal before responding.",
            "PENDING",
            "MEDIUM",
            None,
            18,
        ),
        (
            "TK-0004",
            "Misleading tooltip on Agencies > Plan column",
            "FE shows 'Active plan' tooltip but column actually shows the "
            "current billing tier. Copy tweak needed in components/agencies.",
            "RESOLVED",
            "LOW",
            1,
            30,
        ),
        (
            "TK-0005",
            "Add CSV export to /admin/audit-logs",
            "Compliance asked for CSV download of audit log entries for the "
            "monthly regulator filing. Estimate: 1 day.",
            "CLOSED",
            "MEDIUM",
            None,
            48,
        ),
        (
            "TK-0006",
            "Compliance page: bulk-upload documents UX",
            "Agencies with 20+ required documents want drag-and-drop multi-"
            "file upload instead of one row at a time. Design ticket.",
            "OPEN",
            "HIGH",
            3,
            72,
        ),
        (
            "TK-0007",
            "Production database CPU spiked at 03:12 UTC",
            "P95 query latency tripled. Suspected the new agency_documents "
            "index isn't being used; running EXPLAIN ANALYZE on hot queries.",
            "IN_PROGRESS",
            "CRITICAL",
            None,
            3,
        ),
        (
            "TK-0008",
            "Cosmetic: rename 'PLATFORM_ADMIN' label in admin nav",
            "Marketing wants 'PLATFORM_ADMIN' → 'Team Admin' on the user "
            "menu. Trivial copy change.",
            "PENDING",
            "LOW",
            4,
            96,
        ),
    ]

    agencies = await _pick_agencies(engine, n=10)

    tickets_created = 0
    comments_created = 0

    async with engine.begin() as conn:
        for (
            code,
            title,
            description,
            status,
            priority,
            agency_idx,
            hours_ago,
        ) in ticket_specs:
            agency_id = agencies[agency_idx][0] if agency_idx is not None else None
            created_at = now - timedelta(hours=hours_ago)
            ticket_id = uuid.uuid4()
            # asyncpg uses $N positional params, so we can't mix a bind
            # variable with an inline ::enum cast in the same SQL string.
            # Easiest fix: bind the enum values as their string form and
            # let Postgres cast on insertion (the column is typed, so a
            # string that matches an enum label just works).
            await conn.execute(
                text(
                    "INSERT INTO tickets ("
                    "id, created_at, updated_at, code, title, description, "
                    "status, priority, agency_id, reporter_user_id, "
                    "assignee_user_id, deleted_at, extra"
                    ") VALUES ("
                    ":id, :ca, :ua, :code, :title, :desc, "
                    ":status, :priority, "
                    ":agency_id, :reporter, :assignee, NULL, :extra"
                    ")"
                ),
                {
                    "id": ticket_id,
                    "ca": created_at,
                    "ua": created_at,
                    "code": code,
                    "title": title,
                    "desc": description,
                    "status": status,
                    "priority": priority,
                    "agency_id": agency_id,
                    "reporter": reporter_id,
                    "assignee": reporter_id,  # self-assigned for the demo
                    "extra": json.dumps({}),
                },
            )
            tickets_created += 1

            # Per-ticket comment thread. Mix of COMMENT / STATUS_CHANGE /
            # ASSIGNMENT so the timeline isn't all one kind.
            thread: list[tuple[str, str, dict, int]] = [
                # (kind, body, metadata, minutes_after_ticket_created)
                (
                    "COMMENT",
                    "Reproduced on staging — root cause looks like a stale "
                    "webhook secret. Rotating now.",
                    {},
                    15,
                ),
                (
                    "STATUS_CHANGE",
                    f"OPEN → IN_PROGRESS",
                    {"from": "OPEN", "to": "IN_PROGRESS"},
                    30,
                ),
                (
                    "ASSIGNMENT",
                    f"Assigned to {SUPER_ADMIN_EMAIL}",
                    {"assignee_id": str(reporter_id)},
                    45,
                ),
            ]
            if status in ("RESOLVED", "CLOSED"):
                thread.append(
                    (
                        "STATUS_CHANGE",
                        f"{status} — closing the loop.",
                        {"from": "IN_PROGRESS", "to": status},
                        120,
                    )
                )
            if status == "PENDING":
                thread.append(
                    (
                        "COMMENT",
                        "Waiting on legal response. Will follow up next "
                        "Wednesday.",
                        {},
                        240,
                    )
                )

            for kind, body, metadata, minutes_after in thread:
                comment_id = uuid.uuid4()
                comment_at = created_at + timedelta(minutes=minutes_after)
                await conn.execute(
                    text(
                        "INSERT INTO ticket_comments ("
                        "id, created_at, updated_at, ticket_id, "
                        "author_user_id, kind, body, event_metadata, edited_at"
                        ") VALUES ("
                        ":id, :ca, :ua, :ticket, :author, "
                        ":kind, :body, :meta, NULL"
                        ")"
                    ),
                    {
                        "id": comment_id,
                        "ca": comment_at,
                        "ua": comment_at,
                        "ticket": ticket_id,
                        "author": reporter_id,
                        "kind": kind,
                        "body": body,
                        "meta": json.dumps(metadata or {}),
                    },
                )
                comments_created += 1

    return tickets_created, comments_created


async def _insert_documents(
    engine: AsyncEngine, agencies: list[tuple[uuid.UUID, str]]
) -> int:
    """Insert 12 documents across 4 agencies with a status mix."""
    if len(agencies) < 4:
        raise SystemExit(
            "ERROR: need at least 4 ACTIVE agencies to seed documents. "
            f"Found {len(agencies)}."
        )

    now = _now()

    # (agency_index, name, doc_type, days_from_now, file_url_provided)
    # days_from_now offset for expires_at:
    #   None         → status MISSING (no file, no expiry)
    #   negative     → status EXPIRED
    #   0..7         → status EXPIRING (within 30 days)
    #   90+          → status VALID
    doc_specs: list[tuple[int, str, str, int | None, bool]] = [
        # Agency 1 — health checks
        (0, "State Operating License", "LICENSE", 180, True),
        (0, "HIPAA Privacy Policy", "POLICY", 365, True),
        (0, "Background Check Policy", "POLICY", None, False),
        # Agency 2 — mostly fine, one expired
        (1, "Business License", "LICENSE", -10, True),
        (1, "Workers' Comp Insurance", "POLICY", 200, True),
        (1, "Annual Quality Report", "REPORT", None, False),
        # Agency 3 — two expiring soon
        (2, "Home Health Agency License", "LICENSE", 12, True),
        (2, "Medicare Certification", "CERTIFICATE", 25, True),
        (2, "Emergency Preparedness Plan", "POLICY", None, False),
        # Agency 4 — expired + missing
        (3, "State Operating License", "LICENSE", -45, True),
        (3, "Liability Insurance Certificate", "CERTIFICATE", None, False),
        (3, "Staff Training Manual", "DOCUMENT", 150, True),
    ]

    inserted = 0
    async with engine.begin() as conn:
        for agency_idx, name, doc_type, days, has_file in doc_specs:
            agency_id = agencies[agency_idx][0]
            if days is None and not has_file:
                status = "MISSING"
                expires_at = None
                file_url = None
            elif days is not None and days < 0:
                status = "EXPIRED"
                expires_at = now + timedelta(days=days)
                file_url = f"https://files.example.com/{uuid.uuid4()}.pdf"
            elif days is not None and days <= 30:
                status = "EXPIRING"
                expires_at = now + timedelta(days=days)
                file_url = f"https://files.example.com/{uuid.uuid4()}.pdf"
            else:
                status = "VALID"
                expires_at = now + timedelta(days=days) if days is not None else None
                file_url = f"https://files.example.com/{uuid.uuid4()}.pdf"

            await conn.execute(
                text(
                    "INSERT INTO agency_documents ("
                    "id, created_at, updated_at, deleted_at, agency_id, name, "
                    "doc_type, status, description, expires_at, file_url, extra"
                    ") VALUES ("
                    ":id, now(), now(), NULL, :agency, :name, "
                    ":doc_type, :status, "
                    ":desc, :expires_at, :file_url, :extra"
                    ")"
                ),
                {
                    "id": uuid.uuid4(),
                    "agency": agency_id,
                    "name": name,
                    "doc_type": doc_type,
                    "status": status,
                    "desc": f"Required {doc_type.lower()} for {agencies[agency_idx][1]}.",
                    "expires_at": expires_at,
                    "file_url": file_url,
                    "extra": json.dumps({}),
                },
            )
            inserted += 1

    return inserted


async def _insert_licenses(
    engine: AsyncEngine, agencies: list[tuple[uuid.UUID, str]]
) -> int:
    """Insert 10 licenses across 4 agencies with a status mix."""
    if len(agencies) < 4:
        raise SystemExit(
            "ERROR: need at least 4 ACTIVE agencies to seed licenses. "
            f"Found {len(agencies)}."
        )

    now = _now()

    # (agency_index, name, days_until_expiry, reference_number)
    # days buckets (matches compute_license_status):
    #   <0   → EXPIRED
    #   <=14 → CRITICAL
    #   <=30 → WARNING
    #   <=60 → UPCOMING
    #   >60  → VALID
    lic_specs: list[tuple[int, str, int, str]] = [
        # Agency 1
        (0, "State Operating License", 200, "STATE-OP-001"),
        (0, "Medicare Provider License", 75, "MEDICARE-99821"),
        (0, "Business License", 400, "BUS-2024-118"),
        # Agency 2 — one expired, one critical
        (1, "State Operating License", -20, "STATE-OP-002"),
        (1, "Home Health License", 7, "HHA-2024-77"),
        (1, "DEA Registration", 90, "DEA-B9821"),
        # Agency 3 — warning bucket
        (2, "State Operating License", 21, "STATE-OP-003"),
        (2, "NPI Registration", 365, "NPI-18394756"),
        # Agency 4 — upcoming bucket
        (3, "State Operating License", 45, "STATE-OP-004"),
        (3, "Professional Liability Insurance", 120, "PLI-2024-555"),
    ]

    inserted = 0
    async with engine.begin() as conn:
        for agency_idx, name, days, ref in lic_specs:
            agency_id = agencies[agency_idx][0]
            expires_at = now + timedelta(days=days)
            # Derive status from days so it matches compute_license_status.
            if days < 0:
                status = "EXPIRED"
            elif days <= 14:
                status = "CRITICAL"
            elif days <= 30:
                status = "WARNING"
            elif days <= 60:
                status = "UPCOMING"
            else:
                status = "VALID"

            await conn.execute(
                text(
                    "INSERT INTO agency_licenses ("
                    "id, created_at, updated_at, deleted_at, agency_id, name, "
                    "doc_type, status, issued_at, expires_at, "
                    "reference_number, notes, extra"
                    ") VALUES ("
                    ":id, now(), now(), NULL, :agency, :name, "
                    ":doc_type, :status, "
                    ":issued, :expires, :ref, :notes, :extra"
                    ")"
                ),
                {
                    "id": uuid.uuid4(),
                    "agency": agency_id,
                    "name": name,
                    "doc_type": "LICENSE",
                    "status": status,
                    "issued": now - timedelta(days=365),
                    "expires": expires_at,
                    "ref": ref,
                    "notes": f"License for {agencies[agency_idx][1]}.",
                    "extra": json.dumps({}),
                },
            )
            inserted += 1

    return inserted


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------
async def _seed() -> SeedResult:
    engine = create_async_engine(
        settings.effective_database_url,
        pool_pre_ping=True,
    )
    try:
        try:
            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
        except Exception as exc:
            print(
                f"\nDatabase unreachable: {type(exc).__name__}: {exc}",
                file=sys.stderr,
            )
            raise SystemExit(2) from exc

        print("Looking up super admin…")
        super_id = await _require_super_admin_id(engine)

        print("Wiping existing tickets, comments, documents, licenses…")
        await _wipe(engine)

        print("Picking 4 ACTIVE agencies…")
        agencies = await _pick_agencies(engine, n=4)
        if len(agencies) < 4:
            raise SystemExit(
                f"ERROR: need at least 4 ACTIVE agencies, found {len(agencies)}."
            )

        print("Inserting 8 tickets + comment threads…")
        tickets_n, comments_n = await _insert_tickets(engine, reporter_id=super_id)

        print("Inserting 12 agency documents…")
        documents_n = await _insert_documents(engine, agencies)

        print("Inserting 10 agency licenses…")
        licenses_n = await _insert_licenses(engine, agencies)

        return SeedResult(
            tickets=tickets_n,
            ticket_comments=comments_n,
            documents=documents_n,
            licenses=licenses_n,
        )
    finally:
        await engine.dispose()


def main() -> None:
    print("Seeding admin dashboard demo data…\n")
    result = asyncio.run(_seed())
    print(
        f"\nDone.\n"
        f"  tickets         = {result.tickets}\n"
        f"  ticket_comments = {result.ticket_comments}\n"
        f"  agency_documents= {result.documents}\n"
        f"  agency_licenses = {result.licenses}\n"
    )
    print("Now hit these endpoints to see the data:")
    print("  GET /admin/tickets")
    print("  GET /admin/tickets/stats")
    print("  GET /admin/compliance/documents")
    print("  GET /admin/compliance/licenses")
    print("  GET /admin/compliance/stats")


if __name__ == "__main__":
    main()