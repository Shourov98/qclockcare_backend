"""Reports module — `report_runs` table.

Persists every Claude narrative generation so users can re-read,
re-export, or browse history. Each row stores:

  - `params`              user-supplied filters (date range, etc.)
  - `aggregate_payload`   data snapshot sent to Claude (reused by exports)
  - `narrative`           final streamed text (NULL while streaming)
  - `status`              streaming | completed | failed
  - `claude_model`        which Claude model served this run
  - `input_tokens` /
    `output_tokens` /
    `cost_usd`            cost-tracking for the rate-limit / ops dashboards
  - `error`               captured exception text on `status='failed'`

Indexes:
  - (agency_id, created_at DESC) — primary history list path
  - (requested_by_user_id, created_at DESC) — per-user history
  - (agency_id, report_type, created_at DESC) — filter history by type

RLS mirrors `audit_logs` patterns — agency-scoped reads, AGENCY_ADMIN
writes only within their agency. Unlike `audit_logs`, this table is
*not* append-only — we UPDATE the narrative and status as the run
progresses — so no `audit_logs_no_modify`-style trigger.

Revision ID: 0018_report_runs
Revises: 0017_visit_live_location
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0018_report_runs"
down_revision: str | Sequence[str] | None = "0017_visit_live_location"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ============================================================
    # report_runs
    # ============================================================
    op.create_table(
        "report_runs",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
        ),
        sa.Column(
            "agency_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("agencies.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "requested_by_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "report_type",
            sa.String(length=64),
            nullable=False,
        ),
        sa.Column(
            "params",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "aggregate_payload",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column(
            "narrative",
            sa.Text(),
            nullable=True,
        ),
        sa.Column(
            "status",
            sa.String(length=16),
            nullable=False,
            server_default="streaming",
        ),
        sa.Column(
            "claude_model",
            sa.String(length=64),
            nullable=False,
            server_default="",
        ),
        sa.Column(
            "input_tokens",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "output_tokens",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "cost_usd",
            sa.Numeric(precision=10, scale=6),
            nullable=True,
        ),
        sa.Column(
            "error",
            sa.Text(),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "completed_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.CheckConstraint(
            "status IN ('streaming', 'completed', 'failed')",
            name="ck_report_runs_status",
        ),
        sa.CheckConstraint(
            "report_type <> ''",
            name="ck_report_runs_report_type_non_empty",
        ),
    )
    op.create_index(
        "idx_report_runs_agency_created",
        "report_runs",
        ["agency_id", sa.text("created_at DESC")],
    )
    op.create_index(
        "idx_report_runs_user_created",
        "report_runs",
        ["requested_by_user_id", sa.text("created_at DESC")],
    )
    op.create_index(
        "idx_report_runs_agency_type_created",
        "report_runs",
        ["agency_id", "report_type", sa.text("created_at DESC")],
    )

    # ============================================================
    # RLS — enable + force
    # ============================================================
    # Mirror audit_logs: SUPER_ADMIN sees/writes everything; AGENCY_ADMIN
    # is scoped to their own agency. Unlike audit_logs there's no
    # append-only trigger — we need UPDATEs to write the streamed
    # narrative + transition status to completed/failed.
    op.execute("ALTER TABLE report_runs ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE report_runs FORCE ROW LEVEL SECURITY")

    op.execute(
        """
        CREATE POLICY report_runs_select ON report_runs
        FOR SELECT
        USING (
            app.is_super_admin()
            OR (
                app.has_agency_role('AGENCY_ADMIN')
                AND agency_id = app.current_agency_id()
            )
        )
        """
    )
    op.execute(
        """
        CREATE POLICY report_runs_insert ON report_runs
        FOR INSERT
        WITH CHECK (
            app.is_super_admin()
            OR (
                app.has_agency_role('AGENCY_ADMIN')
                AND agency_id = app.current_agency_id()
            )
        )
        """
    )
    op.execute(
        """
        CREATE POLICY report_runs_update ON report_runs
        FOR UPDATE
        USING (
            app.is_super_admin()
            OR (
                app.has_agency_role('AGENCY_ADMIN')
                AND agency_id = app.current_agency_id()
            )
        )
        WITH CHECK (
            app.is_super_admin()
            OR (
                app.has_agency_role('AGENCY_ADMIN')
                AND agency_id = app.current_agency_id()
            )
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS report_runs_update ON report_runs")
    op.execute("DROP POLICY IF EXISTS report_runs_insert ON report_runs")
    op.execute("DROP POLICY IF EXISTS report_runs_select ON report_runs")
    op.drop_table("report_runs")