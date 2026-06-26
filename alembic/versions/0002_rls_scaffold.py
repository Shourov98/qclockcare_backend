"""RLS scaffold — helper functions + policies for the tables in 0001.

This migration:
- Creates the `app` schema (the home of our RLS helper functions)
- Creates the four `app.current_*` / `app.is_*` helper functions
- Enables RLS on every tenant-scoped table from 0001
- Defines policies for: users, user_roles, agencies, programs,
  agency_programs, auth_audit_events
- Grants the application role access to the helpers and tables

Tables from later migrations (staff_profiles, appointments, etc.) get their
policies added in subsequent migrations, alongside the tables themselves.

RLS reads from session GUCs set by `src.core.database.set_session_context`:
  app.current_user_id     uuid
  app.current_agency_id   uuid
  app.current_user_role   user_role

If none are set, the helpers return NULL, and every policy denies access —
which is the safe default.

Revision ID: 0002_rls
Revises: 0001_base
Create Date: 2026-06-27 03:30:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0002_rls"
down_revision: str | Sequence[str] | None = "0001_base"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ---- App schema for helper functions ----
    op.execute("CREATE SCHEMA IF NOT EXISTS app")
    op.execute("GRANT USAGE ON SCHEMA app TO public")

    # ---- Helper functions ----
    # `current_setting('app.foo', true)` returns NULL if unset (the `true`).
    # Wrapping in NULLIF + cast handles the empty-string case that PG
    # returns for `set_config(..., is_local=true)` before the value is set.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION app.current_user_id()
        RETURNS uuid
        LANGUAGE sql
        STABLE
        AS $$
            SELECT NULLIF(current_setting('app.current_user_id', true), '')::uuid;
        $$;
        """,
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION app.current_agency_id()
        RETURNS uuid
        LANGUAGE sql
        STABLE
        AS $$
            SELECT NULLIF(current_setting('app.current_agency_id', true), '')::uuid;
        $$;
        """,
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION app.current_user_role()
        RETURNS user_role
        LANGUAGE sql
        STABLE
        AS $$
            SELECT NULLIF(current_setting('app.current_user_role', true), '')::user_role;
        $$;
        """,
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION app.is_super_admin()
        RETURNS boolean
        LANGUAGE sql
        STABLE
        AS $$
            SELECT app.current_user_role() = 'SUPER_ADMIN';
        $$;
        """,
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION app.has_agency_role(p_role user_role)
        RETURNS boolean
        LANGUAGE sql
        STABLE
        AS $$
            SELECT EXISTS (
                SELECT 1 FROM user_roles
                WHERE user_id = app.current_user_id()
                  AND role = p_role
                  AND (
                    (p_role = 'SUPER_ADMIN' AND agency_id IS NULL)
                    OR agency_id = app.current_agency_id()
                  )
            );
        $$;
        """
    )

    # Grant execute to public so any role can call the helpers.
    op.execute(
        "GRANT EXECUTE ON FUNCTION app.current_user_id() TO public",
    )
    op.execute(
        "GRANT EXECUTE ON FUNCTION app.current_agency_id() TO public",
    )
    op.execute(
        "GRANT EXECUTE ON FUNCTION app.current_user_role() TO public",
    )
    op.execute(
        "GRANT EXECUTE ON FUNCTION app.is_super_admin() TO public",
    )
    op.execute(
        "GRANT EXECUTE ON FUNCTION app.has_agency_role(user_role) TO public",
    )

    # ---- Enable + force RLS ----
    for table in (
        "agencies",
        "programs",
        "agency_programs",
        "users",
        "user_roles",
        "auth_audit_events",
    ):
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        # FORCE so that even table owners (e.g. the migration runner) are
        # subject to RLS. The bypass happens through SECURITY DEFINER
        # functions or service-role connections, not through ownership.
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")

    # ============================================================
    # Policies
    # ============================================================

    # ---- agencies ----
    # Super Admin: all rows.
    # Anyone authenticated: can see their own agency (by user_roles.agency_id).
    op.execute(
        """
        CREATE POLICY agencies_select ON agencies
        FOR SELECT
        USING (
            app.is_super_admin()
            OR (
                app.current_user_id() IS NOT NULL
                AND EXISTS (
                    SELECT 1 FROM user_roles ur
                    WHERE ur.user_id = app.current_user_id()
                      AND ur.agency_id = agencies.id
                )
            )
        )
        """,
    )
    # INSERT / UPDATE / DELETE: Super Admin only.
    op.execute(
        """
        CREATE POLICY agencies_insert ON agencies
        FOR INSERT
        WITH CHECK (app.is_super_admin())
        """,
    )
    op.execute(
        """
        CREATE POLICY agencies_update ON agencies
        FOR UPDATE
        USING (app.is_super_admin())
        WITH CHECK (app.is_super_admin())
        """,
    )
    op.execute(
        """
        CREATE POLICY agencies_delete ON agencies
        FOR DELETE
        USING (app.is_super_admin())
        """,
    )

    # ---- programs ----
    # Master reference table — read by all authenticated users, write by Super Admin.
    op.execute(
        """
        CREATE POLICY programs_select ON programs
        FOR SELECT
        USING (app.current_user_id() IS NOT NULL)
        """,
    )
    op.execute(
        """
        CREATE POLICY programs_modify ON programs
        FOR ALL
        USING (app.is_super_admin())
        WITH CHECK (app.is_super_admin())
        """,
    )

    # ---- agency_programs ----
    # Junction: read by users who belong to that agency; write by Super Admin
    # or the agency's AGENCY_ADMIN.
    op.execute(
        """
        CREATE POLICY agency_programs_select ON agency_programs
        FOR SELECT
        USING (
            app.is_super_admin()
            OR agency_id = app.current_agency_id()
        )
        """,
    )
    op.execute(
        """
        CREATE POLICY agency_programs_modify ON agency_programs
        FOR ALL
        USING (
            app.is_super_admin()
            OR (
                agency_id = app.current_agency_id()
                AND app.has_agency_role('AGENCY_ADMIN')
            )
        )
        WITH CHECK (
            app.is_super_admin()
            OR (
                agency_id = app.current_agency_id()
                AND app.has_agency_role('AGENCY_ADMIN')
            )
        )
        """,
    )

    # ---- users ----
    # Super Admin: all. AGENCY_ADMIN: users in their agency. Self: own row.
    op.execute(
        """
        CREATE POLICY users_select ON users
        FOR SELECT
        USING (
            app.is_super_admin()
            OR id = app.current_user_id()
            OR (
                app.has_agency_role('AGENCY_ADMIN')
                AND EXISTS (
                    SELECT 1 FROM user_roles ur
                    WHERE ur.user_id = users.id
                      AND ur.agency_id = app.current_agency_id()
                )
            )
        )
        """,
    )
    op.execute(
        """
        CREATE POLICY users_insert ON users
        FOR INSERT
        WITH CHECK (
            app.is_super_admin()
            OR app.has_agency_role('AGENCY_ADMIN')
        )
        """,
    )
    op.execute(
        """
        CREATE POLICY users_update ON users
        FOR UPDATE
        USING (
            app.is_super_admin()
            OR id = app.current_user_id()
            OR (
                app.has_agency_role('AGENCY_ADMIN')
                AND EXISTS (
                    SELECT 1 FROM user_roles ur
                    WHERE ur.user_id = users.id
                      AND ur.agency_id = app.current_agency_id()
                )
            )
        )
        WITH CHECK (
            app.is_super_admin()
            OR id = app.current_user_id()
            OR (
                app.has_agency_role('AGENCY_ADMIN')
                AND EXISTS (
                    SELECT 1 FROM user_roles ur
                    WHERE ur.user_id = users.id
                      AND ur.agency_id = app.current_agency_id()
                )
            )
        )
        """,
    )
    op.execute(
        """
        CREATE POLICY users_delete ON users
        FOR DELETE
        USING (app.is_super_admin())
        """,
    )

    # ---- user_roles ----
    # Super Admin: all. AGENCY_ADMIN: roles in their agency. Self: own rows.
    op.execute(
        """
        CREATE POLICY user_roles_select ON user_roles
        FOR SELECT
        USING (
            app.is_super_admin()
            OR user_id = app.current_user_id()
            OR (
                app.has_agency_role('AGENCY_ADMIN')
                AND agency_id = app.current_agency_id()
            )
        )
        """,
    )
    op.execute(
        """
        CREATE POLICY user_roles_modify ON user_roles
        FOR ALL
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
        """,
    )

    # ---- auth_audit_events ----
    # Super Admin: all. Self: own events. Append-only (UPDATE/DELETE already
    # blocked at the trigger level in 0001).
    op.execute(
        """
        CREATE POLICY auth_audit_events_select ON auth_audit_events
        FOR SELECT
        USING (
            app.is_super_admin()
            OR user_id = app.current_user_id()
        )
        """,
    )
    op.execute(
        """
        CREATE POLICY auth_audit_events_insert ON auth_audit_events
        FOR INSERT
        WITH CHECK (app.current_user_id() IS NOT NULL)
        """,
    )


def downgrade() -> None:
    # Policies drop with their tables; we drop them explicitly for safety.
    for table in (
        "auth_audit_events",
        "user_roles",
        "users",
        "agency_programs",
        "programs",
        "agencies",
    ):
        op.execute(f"DROP TABLE IF EXISTS {table} CASCADE")  # placeholder no-op

    op.execute("DROP POLICY IF EXISTS auth_audit_events_insert ON auth_audit_events")
    op.execute("DROP POLICY IF EXISTS auth_audit_events_select ON auth_audit_events")
    op.execute("DROP POLICY IF EXISTS user_roles_modify ON user_roles")
    op.execute("DROP POLICY IF EXISTS user_roles_select ON user_roles")
    op.execute("DROP POLICY IF EXISTS users_delete ON users")
    op.execute("DROP POLICY IF EXISTS users_update ON users")
    op.execute("DROP POLICY IF EXISTS users_insert ON users")
    op.execute("DROP POLICY IF EXISTS users_select ON users")
    op.execute("DROP POLICY IF EXISTS agency_programs_modify ON agency_programs")
    op.execute("DROP POLICY IF EXISTS agency_programs_select ON agency_programs")
    op.execute("DROP POLICY IF EXISTS programs_modify ON programs")
    op.execute("DROP POLICY IF EXISTS programs_select ON programs")
    op.execute("DROP POLICY IF EXISTS agencies_delete ON agencies")
    op.execute("DROP POLICY IF EXISTS agencies_update ON agencies")
    op.execute("DROP POLICY IF EXISTS agencies_insert ON agencies")
    op.execute("DROP POLICY IF EXISTS agencies_select ON agencies")

    for table in (
        "auth_audit_events",
        "user_roles",
        "users",
        "agency_programs",
        "programs",
        "agencies",
    ):
        op.execute(f"ALTER TABLE IF EXISTS {table} NO FORCE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE IF EXISTS {table} DISABLE ROW LEVEL SECURITY")

    op.execute("DROP FUNCTION IF EXISTS app.has_agency_role(user_role)")
    op.execute("DROP FUNCTION IF EXISTS app.is_super_admin()")
    op.execute("DROP FUNCTION IF EXISTS app.current_user_role()")
    op.execute("DROP FUNCTION IF EXISTS app.current_agency_id()")
    op.execute("DROP FUNCTION IF EXISTS app.current_user_id()")
    op.execute("DROP SCHEMA IF EXISTS app CASCADE")