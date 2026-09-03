"""Rename `visit_activity_deliveries.appointment_service_item_id` → `activity_id`.

Why
---
Migration 0027_appointment_flow_alignment renamed the *table*
`visit_service_items` → `visit_activity_deliveries` to match the new
ORM model name (see `src/modules/visits/models.py:344`). It forgot
to rename the column though — the DB still has
`appointment_service_item_id` (legacy `service_item` pluralisation),
but the ORM (`VisitActivityDelivery.activity_id`) maps to `activity_id`.

Net effect: every read of `visit_activity_deliveries` raised
`UndefinedColumnError: column visit_activity_deliveries.activity_id
does not exist` — visible to the FE on
`GET /visits/{id}/with-items`, `GET /visits/{id}/activities`,
`GET /portal/visits/{id}`, `GET /patients/{id}/visits/history`.

This migration:
  1. Drops the unique index `uq_visit_service_item` (it references
     the old column name in its definition — Postgres renames the
     index automatically when we rename the column, but the index
     *name* is keyed off the legacy table name; renaming the index
     keeps `pg_indexes` tidy).
  2. Drops the FK constraint
     `fk_visit_service_items_appointment_service_item_id_appo_ca03`
     (the FK was created against the OLD table/column name in 0007;
     it needs to be re-created against the new column name).
  3. Renames the column with `ALTER TABLE ... RENAME COLUMN ...`.
  4. Re-creates the unique index against the new column.
  5. Re-creates the FK constraint against the new column.
  6. Drops the unused `appointment_activities.appointment_service_item_id`
     column — it was a self-FK to the legacy `appointment_service_items`
     table; after the table rename to `appointment_activities` it became
     a self-loop pointing to the same row, which the ORM never reads.
     Keeping it would just confuse future schema diffs.

Revision ID: 0031_visit_activity_deliveries_activity_id_rename
Revises:    0030_compliance_issues
"""

from __future__ import annotations

from alembic import op


revision = "0031_visit_activity_deliveries_activity_id_rename"
down_revision = "0030_compliance_issues"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. Drop the legacy UNIQUE constraint (Postgres auto-promoted the
    #    unique index to a constraint at some point — `DROP INDEX`
    #    fails with `cannot drop index because constraint requires it`,
    #    so drop the constraint first).
    op.execute(
        "ALTER TABLE visit_activity_deliveries "
        "DROP CONSTRAINT IF EXISTS uq_visit_service_item"
    )

    # 2. Drop the old FK — its name was generated from the legacy
    #    column name in 0007_visit_verification.py.
    op.execute(
        "ALTER TABLE visit_activity_deliveries "
        "DROP CONSTRAINT IF EXISTS "
        "fk_visit_service_items_appointment_service_item_id_appo_ca03"
    )

    # 3. Rename the column.
    op.execute(
        "ALTER TABLE visit_activity_deliveries "
        "RENAME COLUMN appointment_service_item_id TO activity_id"
    )

    # 4. Add the missing `agency_id` column.
    #    Migration 0027 renamed the table to `visit_activity_deliveries`
    #    but forgot to add the `agency_id` column that the new ORM
    #    (`VisitActivityDelivery.agency_id`) requires for RLS scoping.
    #    Backfill from the parent visit row so existing data satisfies
    #    the new NOT NULL constraint.
    op.execute(
        "ALTER TABLE visit_activity_deliveries "
        "ADD COLUMN agency_id uuid"
    )
    op.execute(
        "UPDATE visit_activity_deliveries vad "
        "SET agency_id = v.agency_id "
        "FROM visits v "
        "WHERE vad.visit_id = v.id AND vad.agency_id IS NULL"
    )
    op.execute(
        "ALTER TABLE visit_activity_deliveries "
        "ALTER COLUMN agency_id SET NOT NULL"
    )
    op.execute(
        "ALTER TABLE visit_activity_deliveries "
        "ADD CONSTRAINT fk_visit_activity_deliveries_agency_id "
        "FOREIGN KEY (agency_id) REFERENCES agencies(id) "
        "ON DELETE CASCADE"
    )

    # 5. Re-create the unique constraint — `UNIQUE (visit_id, activity_id)`.
    op.execute(
        "ALTER TABLE visit_activity_deliveries "
        "ADD CONSTRAINT uq_visit_activity_delivery "
        "UNIQUE (visit_id, activity_id)"
    )

    # 6. Re-create the FK against `appointment_activities.id`.
    op.execute(
        "ALTER TABLE visit_activity_deliveries "
        "ADD CONSTRAINT fk_visit_activity_deliveries_activity_id "
        "FOREIGN KEY (activity_id) REFERENCES appointment_activities(id) "
        "ON DELETE CASCADE"
    )

    # 7. Drop the now-unused self-FK column on `appointment_activities`.
    #    (Was `appointment_service_item_id` — pointed at the legacy
    #    `appointment_service_items` table; after 0027 the table was
    #    renamed to `appointment_activities` and the column became a
    #    self-loop the ORM never reads.)
    op.execute(
        "ALTER TABLE appointment_activities "
        "DROP COLUMN IF EXISTS appointment_service_item_id"
    )


def downgrade() -> None:
    # Reverse: re-add the self-FK column on appointment_activities.
    op.execute(
        "ALTER TABLE appointment_activities "
        "ADD COLUMN IF NOT EXISTS appointment_service_item_id uuid"
    )
    # Drop the new FK + unique constraint + agency_id column.
    op.execute(
        "ALTER TABLE visit_activity_deliveries "
        "DROP CONSTRAINT IF EXISTS fk_visit_activity_deliveries_activity_id"
    )
    op.execute(
        "ALTER TABLE visit_activity_deliveries "
        "DROP CONSTRAINT IF EXISTS fk_visit_activity_deliveries_agency_id"
    )
    op.execute(
        "ALTER TABLE visit_activity_deliveries "
        "DROP CONSTRAINT IF EXISTS uq_visit_activity_delivery"
    )
    op.execute(
        "ALTER TABLE visit_activity_deliveries "
        "DROP COLUMN IF EXISTS agency_id"
    )
    # Rename column back.
    op.execute(
        "ALTER TABLE visit_activity_deliveries "
        "RENAME COLUMN activity_id TO appointment_service_item_id"
    )
    # Recreate the legacy FK constraint with its legacy name.
    op.execute(
        "ALTER TABLE visit_activity_deliveries "
        "ADD CONSTRAINT fk_visit_service_items_appointment_service_item_id_appo_ca03 "
        "FOREIGN KEY (appointment_service_item_id) "
        "REFERENCES appointment_activities(id) "
        "ON DELETE CASCADE"
    )
    # Recreate the legacy unique constraint.
    op.execute(
        "ALTER TABLE visit_activity_deliveries "
        "ADD CONSTRAINT uq_visit_service_item "
        "UNIQUE (visit_id, appointment_service_item_id)"
    )