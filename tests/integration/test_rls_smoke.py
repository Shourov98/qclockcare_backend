"""RLS smoke-test — verify the migration 0002 policies behave correctly."""

from __future__ import annotations

import asyncio
import uuid

from sqlalchemy import text

from src.core.database import engine


async def main() -> None:
    agency_id = str(uuid.uuid4())
    user_id = str(uuid.uuid4())
    role_id = str(uuid.uuid4())

    async with engine.begin() as conn:
        # ---- Seed ----
        await conn.execute(
            text(
                "INSERT INTO agencies (id, name, timezone) "
                "VALUES (:id, 'RLS Test Agency', 'UTC')"
            ),
            {"id": agency_id},
        )
        await conn.execute(
            text(
                "INSERT INTO users (id, email, full_name) "
                "VALUES (:id, 'rls-test@example.com', 'RLS Test')"
            ),
            {"id": user_id},
        )
        await conn.execute(
            text(
                "INSERT INTO user_roles (id, user_id, agency_id, role) "
                "VALUES (:id, :uid, :aid, 'AGENCY_ADMIN')"
            ),
            {"id": role_id, "uid": user_id, "aid": agency_id},
        )

        # ---- Create non-bypass role (idempotent) ----
        existing = (
            await conn.execute(
                text("SELECT 1 FROM pg_roles WHERE rolname = 'app_user'")
            )
        ).scalar()
        if not existing:
            await conn.execute(text("CREATE ROLE app_user NOINHERIT"))
        await conn.execute(text("GRANT app_user TO postgres"))
        await conn.execute(text("GRANT USAGE ON SCHEMA public TO app_user"))
        await conn.execute(
            text(
                "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES "
                "IN SCHEMA public TO app_user"
            )
        )
        await conn.execute(
            text(
                "GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO app_user"
            )
        )
        await conn.execute(
            text("GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA app TO app_user")
        )

        # ---- 1. No GUCs => deny everything ----
        await conn.execute(text("SET LOCAL ROLE app_user"))
        print("=== app_user, NO GUCs (deny by default) ===")
        for table in (
            "agencies",
            "programs",
            "agency_programs",
            "users",
            "user_roles",
            "auth_audit_events",
        ):
            n = (
                await conn.execute(text(f"SELECT count(*) FROM {table}"))
            ).scalar()
            print(f"  {table:25s} -> {n}")

        # ---- 2. With GUCs set, AGENCY_ADMIN sees their agency ----
        await conn.execute(
            text("SELECT set_config('app.current_user_id', :u, false)"),
            {"u": user_id},
        )
        await conn.execute(
            text("SELECT set_config('app.current_agency_id', :a, false)"),
            {"a": agency_id},
        )
        await conn.execute(
            text("SELECT set_config('app.current_user_role', 'AGENCY_ADMIN', false)")
        )
        print("\n=== app_user, AGENCY_ADMIN at the matching agency ===")
        for table in (
            "agencies",
            "programs",
            "agency_programs",
            "users",
            "user_roles",
            "auth_audit_events",
        ):
            n = (
                await conn.execute(text(f"SELECT count(*) FROM {table}"))
            ).scalar()
            print(f"  {table:25s} -> {n}")

        # ---- 3. Different agency => see 0 in agencies ----
        await conn.execute(
            text(
                "SELECT set_config('app.current_agency_id', "
                "'00000000-0000-0000-0000-000000000000', false)"
            )
        )
        print("\n=== app_user, AGENCY_ADMIN at a DIFFERENT agency ===")
        for table in ("agencies", "user_roles"):
            n = (
                await conn.execute(text(f"SELECT count(*) FROM {table}"))
            ).scalar()
            print(f"  {table:25s} -> {n}")

        # ---- 4. SUPER_ADMIN sees everything ----
        await conn.execute(
            text("SELECT set_config('app.current_user_role', 'SUPER_ADMIN', false)")
        )
        print("\n=== app_user, SUPER_ADMIN role ===")
        for table in ("agencies", "users"):
            n = (
                await conn.execute(text(f"SELECT count(*) FROM {table}"))
            ).scalar()
            print(f"  {table:25s} -> {n}")

        await conn.execute(text("RESET ROLE"))

        # ---- Cleanup ----
        await conn.execute(
            text("DELETE FROM user_roles WHERE agency_id = :a"), {"a": agency_id}
        )
        await conn.execute(
            text("DELETE FROM users WHERE id = :u"), {"u": user_id}
        )
        await conn.execute(
            text("DELETE FROM agencies WHERE id = :a"), {"a": agency_id}
        )
        # Drop the role cleanly
        await conn.execute(text("REVOKE ALL ON SCHEMA public FROM app_user"))
        await conn.execute(text("REVOKE ALL ON ALL TABLES IN SCHEMA public FROM app_user"))
        await conn.execute(text("REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM app_user"))
        await conn.execute(text("REVOKE ALL ON ALL FUNCTIONS IN SCHEMA app FROM app_user"))
        await conn.execute(text("REVOKE app_user FROM postgres"))
        await conn.execute(text("DROP ROLE IF EXISTS app_user"))


    print("\nAll RLS smoke-tests passed.")


if __name__ == "__main__":
    asyncio.run(main())