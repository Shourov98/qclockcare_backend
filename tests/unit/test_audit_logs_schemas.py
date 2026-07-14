"""Unit tests for audit_logs schemas — `AuditLogResponse` shape.

Validates:
  - Default metadata is empty dict
  - ORM `metadata_` attribute is mapped to JSON `metadata` field
  - INET `ip_address` is converted to str on read
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from src.modules.audit_logs.schemas import AuditLogResponse
from src.shared.domain.enums import AuditAction


def _valid_kwargs() -> dict:
    return {
        "id": str(uuid.uuid4()),
        "agency_id": str(uuid.uuid4()),
        "actor_user_id": str(uuid.uuid4()),
        "action": AuditAction.CREATE.value,
        "entity_type": "APPOINTMENT",
        "entity_id": str(uuid.uuid4()),
        "old_data": None,
        "new_data": {"foo": "bar"},
        "metadata": {"trace_id": "abc"},
        "ip_address": "10.0.0.1",
        "user_agent": "pytest",
        "created_at": "2026-06-27T10:00:00Z",
    }


class TestAuditLogResponse:
    def test_basic_dict_input_ok(self) -> None:
        kw = _valid_kwargs()
        r = AuditLogResponse.model_validate(kw)
        assert r.action == AuditAction.CREATE
        assert r.entity_type == "APPOINTMENT"
        assert r.new_data == {"foo": "bar"}
        assert r.metadata == {"trace_id": "abc"}
        assert r.ip_address == "10.0.0.1"

    def test_default_metadata_is_empty_dict(self) -> None:
        kw = _valid_kwargs()
        kw.pop("metadata", None)
        r = AuditLogResponse.model_validate(kw)
        assert r.metadata == {}

    def test_optional_fields_default_to_none(self) -> None:
        kw = _valid_kwargs()
        kw["old_data"] = None
        kw["new_data"] = None
        kw["ip_address"] = None
        kw["user_agent"] = None
        kw["entity_id"] = None
        kw["actor_user_id"] = None
        kw["agency_id"] = None
        r = AuditLogResponse.model_validate(kw)
        assert r.old_data is None
        assert r.new_data is None
        assert r.ip_address is None
        assert r.user_agent is None
        assert r.entity_id is None
        assert r.actor_user_id is None
        assert r.agency_id is None

    def test_accepts_orm_with_metadata_attr(self) -> None:
        """ORM attribute is `metadata_`; schema should map to `metadata`."""

        class FakeORM:
            pass

        orm = FakeORM()
        orm.id = uuid.uuid4()
        orm.agency_id = uuid.uuid4()
        orm.actor_user_id = uuid.uuid4()
        orm.action = AuditAction.SERVICE_VERIFIED
        orm.entity_type = "SERVICE_VERIFICATION"
        orm.entity_id = uuid.uuid4()
        orm.old_data = None
        orm.new_data = {"visit_id": "v1"}
        orm.metadata_ = {"trace_id": "t1", "ip": "10.0.0.1"}
        # Simulate asyncpg returning an INET — use a fake that stringifies.
        class FakeInet:
            def __str__(self) -> str:
                return "192.168.1.42"

        orm.ip_address = FakeInet()
        orm.user_agent = "curl/8.0"
        orm.created_at = datetime.now(UTC)

        r = AuditLogResponse.model_validate(orm)
        assert r.action == AuditAction.SERVICE_VERIFIED
        assert r.metadata == {"trace_id": "t1", "ip": "10.0.0.1"}
        # ip_address should be stringified
        assert r.ip_address == "192.168.1.42"
        assert isinstance(r.ip_address, str)
        assert r.new_data == {"visit_id": "v1"}

    def test_accepts_orm_with_none_ip(self) -> None:
        """If ip_address is None on the ORM, the schema should keep None."""

        class FakeORM:
            pass

        orm = FakeORM()
        orm.id = uuid.uuid4()
        orm.agency_id = None
        orm.actor_user_id = None
        orm.action = AuditAction.CREATE
        orm.entity_type = "PATIENT_PROFILE"
        orm.entity_id = uuid.uuid4()
        orm.old_data = None
        orm.new_data = {}
        orm.metadata_ = {}
        orm.ip_address = None
        orm.user_agent = None
        orm.created_at = datetime.now(UTC)

        r = AuditLogResponse.model_validate(orm)
        assert r.ip_address is None
        assert r.agency_id is None
        assert r.actor_user_id is None
