"""Unit tests for the `AuditAction.READ` enum member.

`READ` was added to the Python `AuditAction` enum in
`src/shared/domain/enums.py` and to the corresponding Postgres
`audit_action` ENUM type via migration `0013_audit_action_read.py`.
This test guards against accidental removal — without `READ`, any
read-only audit log call (e.g. a future staff-qualification download
audit row) would raise `ValueError` at the SQLAlchemy layer because
the Python enum would no longer recognise the value.

Also covers:
- The string value is exactly `"READ"` (matches the migration
  literal and the Postgres ENUM label).
- It's a valid `StrEnum`, so JSON serialisation round-trips.
- The enum is still usable in the audit_logs ORM column
  declaration (`AuditAction` is the declared type at
  `src/modules/audit_logs/models.py:51`).
- It co-exists with the existing CRUD members (CREATE / UPDATE /
  DELETE), so adding `READ` didn't drop one.
"""

from __future__ import annotations


class TestAuditActionRead:
    def test_read_member_exists(self) -> None:
        """`AuditAction.READ` must be importable and have the
        string value `"READ"` — the migration uses this same
        literal in `ALTER TYPE … ADD VALUE 'READ'`."""
        from src.shared.domain.enums import AuditAction

        assert hasattr(AuditAction, "READ"), (
            "AuditAction.READ is missing — add it next to "
            "STATUS_TRANSITION in src/shared/domain/enums.py "
            "and run migration 0013_audit_action_read."
        )
        assert AuditAction.READ.value == "READ"

    def test_read_member_is_a_strenum(self) -> None:
        """`READ` must be a `StrEnum` member so that
        `model_dump()` and JSON round-trips keep it as a plain
        string in API responses (not the `AuditAction.READ`
        repr)."""
        from src.shared.domain.enums import AuditAction

        assert isinstance(AuditAction.READ, str)
        # StrEnum members compare equal to their string value.
        assert AuditAction.READ == "READ"

    def test_read_member_serialises_to_string(self) -> None:
        """pydantic v2 expects enum values to JSON-serialise as
        the string label, not the enum repr. Verify the StrEnum
        contract holds for the new member."""
        import json

        from src.shared.domain.enums import AuditAction

        # `default=str` is the fallback pydantic uses when it
        # encounters something it doesn't know how to serialise —
        # if `READ` weren't a proper StrEnum, this would emit
        # `AuditAction.READ` instead of `"READ"`.
        serialised = json.dumps(AuditAction.READ, default=str)
        assert serialised == '"READ"'

    def test_crud_members_still_present(self) -> None:
        """Adding `READ` must not have accidentally removed any
        of the existing CRUD members — those are used in dozens
        of router endpoints and removing one would break them."""
        from src.shared.domain.enums import AuditAction

        for required in (
            AuditAction.CREATE,
            AuditAction.UPDATE,
            AuditAction.DELETE,
            AuditAction.STATUS_TRANSITION,
            AuditAction.READ,
            AuditAction.LOGIN,
            AuditAction.LOGOUT,
            AuditAction.LOGIN_FAILED,
        ):
            assert required.value, f"{required.name} is missing its value"

    def test_read_member_count_grew_by_one(self) -> None:
        """Documenting the canonical count guards against
        accidental enum member removal in unrelated PRs — if
        someone removes `STATUS_TRANSITION`, this test fires."""
        from src.shared.domain.enums import AuditAction

        members = list(AuditAction)
        # 20 pre-existing members + 1 new READ = 21.
        assert len(members) == 21, (
            f"expected 21 AuditAction members (20 existing + READ), "
            f"got {len(members)}: {[m.name for m in members]}"
        )


__all__ = ["TestAuditActionRead"]
