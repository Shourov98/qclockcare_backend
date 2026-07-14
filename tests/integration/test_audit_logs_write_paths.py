"""Verify audit_log() helper persists rows in the DB.

Proof that the audit_log() helper actually writes to the audit_logs
table with the expected shape (action, entity_type, actor_user_id,
metadata, INET ip_address, user_agent).
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.core.config import settings

pytestmark = pytest.mark.asyncio


def _make_test_engine():
    return create_async_engine(
        settings.effective_database_url,
        pool_pre_ping=False,
        pool_size=2,
    )


async def _db_reachable(test_engine) -> bool:
    try:
        async with test_engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


async def _seed_agency_admin(session_factory):
    """Seed an agency + AGENCY_ADMIN user inside one session."""
    from src.core.security import hash_password

    password = "TestPass123!AB"
    email = f"test-admin-{uuid.uuid4().hex[:8]}@example.com"

    async with session_factory() as session, session.begin():
        agency_id = str(uuid.uuid4())
        admin_id = str(uuid.uuid4())
        role_id = str(uuid.uuid4())
        await session.execute(
            text(
                "INSERT INTO agencies (id, name, timezone) "
                "VALUES (:id, :name, 'America/Chicago')"
            ),
            {"id": agency_id, "name": f"Audit Test Agency {uuid.uuid4().hex[:6]}"},
        )
        await session.execute(
            text(
                "INSERT INTO users (id, email, password_hash, full_name, status, email_verified_at) "
                "VALUES (:id, :email, :pw, 'Audit Test Admin', 'ACTIVE', now())"
            ),
            {"id": admin_id, "email": email, "pw": hash_password(password)},
        )
        await session.execute(
            text(
                "INSERT INTO user_roles (id, user_id, agency_id, role) "
                "VALUES (:id, :uid, :aid, 'AGENCY_ADMIN')"
            ),
            {"id": role_id, "uid": admin_id, "aid": agency_id},
        )
    return admin_id, agency_id


async def _cleanup(session_factory, agency_id: str) -> None:
    async with session_factory() as session, session.begin():
        await session.execute(text("SET LOCAL session_replication_role = 'replica'"))
        await session.execute(
            text("DELETE FROM audit_logs WHERE agency_id = :a"), {"a": agency_id}
        )
        await session.execute(
            text("DELETE FROM user_roles WHERE agency_id = :a"), {"a": agency_id}
        )
        await session.execute(
            text("DELETE FROM agencies WHERE id = :a"), {"a": agency_id}
        )
        await session.execute(
            text("DELETE FROM users WHERE email LIKE 'test-admin-%@example.com'")
        )


async def test_audit_helper_appends_row_directly() -> None:
    """audit_log() writes a row we can read back with the expected shape."""
    test_engine = _make_test_engine()
    try:
        if not await _db_reachable(test_engine):
            pytest.skip("Database not reachable")
        session_factory = async_sessionmaker(test_engine, expire_on_commit=False)

        admin_id, agency_id = await _seed_agency_admin(session_factory)

        # Import all model modules so mappers register correctly.
        from src.main import app  # noqa: F401
        from src.modules.audit_logs.service import audit_log
        from src.shared.domain.enums import AuditAction

        async with session_factory() as session:
            async with session.begin():
                row = await audit_log(
                    session,
                    agency_id=uuid.UUID(agency_id),
                    actor_user_id=uuid.UUID(admin_id),
                    action=AuditAction.CREATE,
                    entity_type="TEST_ENTITY",
                    entity_id=uuid.uuid4(),
                    new_data={"test": "data"},
                    metadata={"trace": "abc"},
                )
                row_id = row.id

            result = await session.execute(
                text(
                    "SELECT action, entity_type, actor_user_id, metadata "
                    "FROM audit_logs WHERE id = :i"
                ),
                {"i": str(row_id)},
            )
            row_data = result.fetchone()

        assert row_data is not None, "audit row not found"
        action, entity_type, actor_id, metadata = row_data
        assert action == "CREATE"
        assert entity_type == "TEST_ENTITY"
        assert str(actor_id) == admin_id
        assert "trace" in (metadata or {})

        await _cleanup(session_factory, agency_id)
    finally:
        await test_engine.dispose()


async def test_audit_ip_address_persists() -> None:
    """The INET column should accept and store IPv4 addresses."""
    test_engine = _make_test_engine()
    try:
        if not await _db_reachable(test_engine):
            pytest.skip("Database not reachable")
        session_factory = async_sessionmaker(test_engine, expire_on_commit=False)

        admin_id, agency_id = await _seed_agency_admin(session_factory)

        from src.main import app  # noqa: F401
        from src.modules.audit_logs.service import audit_log
        from src.shared.domain.enums import AuditAction

        async with session_factory() as session:
            async with session.begin():
                row = await audit_log(
                    session,
                    agency_id=uuid.UUID(agency_id),
                    actor_user_id=uuid.UUID(admin_id),
                    action=AuditAction.LOGIN,
                    entity_type="USER",
                    entity_id=uuid.UUID(admin_id),
                    ip_address="203.0.113.42",
                    user_agent="pytest/1.0",
                )
                row_id = row.id

            result = await session.execute(
                text("SELECT ip_address, user_agent FROM audit_logs WHERE id = :i"),
                {"i": str(row_id)},
            )
            ip, ua = result.fetchone()

        assert str(ip) == "203.0.113.42"
        assert ua == "pytest/1.0"

        await _cleanup(session_factory, agency_id)
    finally:
        await test_engine.dispose()


async def test_audit_metadata_defaults_to_empty_dict() -> None:
    """If metadata_ is omitted, the row should default to `{}`."""
    test_engine = _make_test_engine()
    try:
        if not await _db_reachable(test_engine):
            pytest.skip("Database not reachable")
        session_factory = async_sessionmaker(test_engine, expire_on_commit=False)

        admin_id, agency_id = await _seed_agency_admin(session_factory)

        from src.main import app  # noqa: F401
        from src.modules.audit_logs.service import audit_log
        from src.shared.domain.enums import AuditAction

        async with session_factory() as session:
            async with session.begin():
                row = await audit_log(
                    session,
                    agency_id=uuid.UUID(agency_id),
                    actor_user_id=uuid.UUID(admin_id),
                    action=AuditAction.UPDATE,
                    entity_type="STAFF_PROFILE",
                    entity_id=uuid.uuid4(),
                )
                row_id = row.id

            result = await session.execute(
                text("SELECT metadata FROM audit_logs WHERE id = :i"),
                {"i": str(row_id)},
            )
            metadata = result.scalar_one()

        # Empty dict round-trips through jsonb
        assert metadata == {}

        await _cleanup(session_factory, agency_id)
    finally:
        await test_engine.dispose()
