"""extensions, enums, identity + agency tables (migration 1)

Creates the foundation of the QlockCare schema:

- Extensions: pgcrypto (gen_random_uuid), citext (case-insensitive email)
- All 19 Postgres ENUM types from 13_DATABASE_SCHEMA_COMPLETE.md §2
- Identity tables: users, user_roles, email_verification_otps, auth_audit_events
- Agency tables: agencies, programs (seeded), agency_programs
- The set_updated_at trigger function — attached to every table with updated_at
- An append-only trigger on auth_audit_events (ADR-0016)

RLS policies are NOT enabled in this migration — they land in a later one
once the auth module + middleware are in place. Setting up RLS in the same
migration as the tables would block running migrations against a non-app
connection (e.g. psql for ops).

See:
- 13_DATABASE_SCHEMA_COMPLETE.md §2-5
- 25_AUTH_AND_HOSTING_DECISIONS.md §7 (auth-specific tables)

Revision ID: 0001_extensions_enums_identity_agencies
Revises:
Create Date: 2026-06-27 00:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from src.shared.domain.enum_mapping import ENUM_TYPE_NAMES

# revision identifiers, used by Alembic.
revision: str = "0001_extensions_enums_identity_agencies"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def _create_enum(name: str, values: list[str]) -> None:
    """Create a Postgres ENUM type. `values` is the ordered list of labels."""
    labels = ", ".join(f"'{v}'" for v in values)
    op.execute(f"CREATE TYPE {name} AS ENUM ({labels})")


def _drop_enum(name: str) -> None:
    op.execute(f"DROP TYPE IF EXISTS {name} CASCADE")


# --------------------------------------------------------------------------
# Upgrade
# --------------------------------------------------------------------------
def upgrade() -> None:
    # ---- Extensions ----
    op.execute('CREATE EXTENSION IF NOT EXISTS "pgcrypto"')
    op.execute('CREATE EXTENSION IF NOT EXISTS "citext"')

    # ---- Enums (in dependency order — user_status references user_role's
    #      semantics; agency / program / appointment enums are independent) ----
    _create_enum(
        "user_role",
        [
            "SUPER_ADMIN",
            "AGENCY_ADMIN",
            "STAFF",
            "PATIENT",
            "GUARDIAN",
        ],
    )
    _create_enum(
        "user_status",
        [
            "INVITED",
            "EMAIL_VERIFICATION_PENDING",
            "ACTIVE",
            "INACTIVE",
            "LOCKED",
            "ARCHIVED",
        ],
    )
    _create_enum("agency_status", ["ACTIVE", "TRIAL", "SUSPENDED", "CHURNED"])
    _create_enum("program_type", ["PCA", "CFSS", "245D", "ARMHS", "COUNSELING"])
    _create_enum(
        "service_type",
        [
            "PERSONAL_CARE",
            "HOMEMAKING",
            "RESPITE",
            "SKILLED_NURSING",
            "MENTAL_HEALTH",
            "COUNSELING_INDIVIDUAL",
            "COUNSELING_GROUP",
        ],
    )
    _create_enum(
        "appointment_status",
        [
            "DRAFT",
            "SCHEDULED",
            "NOTIFICATION_SENT",
            "AWAITING_CONFIRMATION",
            "CONFIRMED",
            "RESCHEDULE_REQUESTED",
            "CANCELLATION_REQUESTED",
            "ASSIGNED",
            "CHECKED_IN",
            "IN_PROGRESS",
            "CHECKED_OUT",
            "COMPLETED",
            "AWAITING_SERVICE_VERIFICATION",
            "SERVICE_VERIFIED",
            "DISPUTED",
            "UNDER_REVIEW",
            "APPROVED_FOR_BILLING",
            "PAID",
            "CANCELLED",
            "NO_SHOW",
            "REJECTED",
        ],
    )
    _create_enum(
        "service_item_status",
        [
            "PENDING",
            "DONE",
            "NOT_DONE",
            "NOT_APPLICABLE",
            "NEEDS_FOLLOW_UP",
        ],
    )
    _create_enum(
        "visit_status",
        [
            "CHECKED_IN",
            "IN_PROGRESS",
            "CHECKED_OUT",
            "COMPLETED",
        ],
    )
    _create_enum("confirmation_status", ["CONFIRMED", "DECLINED"])
    _create_enum("verification_status", ["VERIFIED", "DISPUTED"])
    _create_enum(
        "dispute_reason_code",
        [
            "STAFF_NEVER_ARRIVED",
            "STAFF_ARRIVED_LATE",
            "STAFF_LEFT_EARLY",
            "SERVICE_NOT_COMPLETED",
            "WRONG_SERVICE_MARKED_DONE",
            "WRONG_NOTE",
            "POOR_SERVICE",
            "OTHER",
        ],
    )
    _create_enum("notification_channel", ["IN_APP", "EMAIL", "SMS", "PUSH"])
    _create_enum(
        "notification_status",
        [
            "PENDING",
            "SENT",
            "DELIVERED",
            "FAILED",
            "BOUNCED",
            "READ",
        ],
    )
    _create_enum(
        "notification_type",
        [
            "APPOINTMENT_CREATED",
            "APPOINTMENT_ASSIGNED",
            "APPOINTMENT_CONFIRMED",
            "APPOINTMENT_RESCHEDULE_REQUESTED",
            "APPOINTMENT_CANCELLATION_REQUESTED",
            "APPOINTMENT_CANCELLED",
            "VISIT_CHECK_IN_REMINDER",
            "VISIT_CHECKED_IN",
            "VISIT_CHECKED_OUT",
            "SERVICE_VERIFIED",
            "SERVICE_DISPUTED",
            "STAFF_INVITATION",
            "PASSWORD_RESET",
            "GENERIC",
        ],
    )
    _create_enum(
        "audit_action",
        [
            "CREATE",
            "UPDATE",
            "DELETE",
            "STATUS_TRANSITION",
            "LOGIN",
            "LOGOUT",
            "LOGIN_FAILED",
            "ROLE_GRANTED",
            "ROLE_REVOKED",
            "LINK_PATIENT_GUARDIAN",
            "UNLINK_PATIENT_GUARDIAN",
            "APPOINTMENT_CONFIRMED",
            "APPOINTMENT_RESCHEDULE_REQUESTED",
            "APPOINTMENT_CANCELLATION_REQUESTED",
            "APPOINTMENT_CANCELLED",
            "APPOINTMENT_ASSIGNED",
            "VISIT_CHECKED_IN",
            "VISIT_CHECKED_OUT",
            "SERVICE_VERIFIED",
            "SERVICE_DISPUTED",
        ],
    )
    _create_enum(
        "auth_audit_event_type",
        [
            "INVITATION_SENT",
            "INVITATION_ACCEPTED",
            "INVITATION_EXPIRED",
            "PASSWORD_SET",
            "OTP_SENT",
            "OTP_RESENT",
            "OTP_VERIFIED",
            "OTP_FAILED",
            "OTP_LOCKED",
            "OTP_EXPIRED",
            "EMAIL_VERIFIED",
            "LOGIN_SUCCESS",
            "LOGIN_FAILED",
            "ACCOUNT_LOCKED",
            "ACCOUNT_UNLOCKED",
            "PASSWORD_CHANGED",
            "PASSWORD_RESET_REQUESTED",
            "PASSWORD_RESET_COMPLETED",
            "TOKEN_REFRESHED",
            "TOKEN_REVOKED",
        ],
    )
    _create_enum(
        "relationship_type",
        [
            "SELF",
            "SPOUSE",
            "PARENT",
            "CHILD",
            "SON",
            "DAUGHTER",
            "SIBLING",
            "GRANDPARENT",
            "GRANDCHILD",
            "FRIEND",
            "GUARDIAN",
            "CONSERVATOR",
            "CASEWORKER",
            "POWER_OF_ATTORNEY",
            "OTHER",
        ],
    )
    _create_enum(
        "qualification_type",
        [
            "PCA_CERTIFIED",
            "CFSS_TRAINED",
            "RN",
            "LPN",
            "CNA",
            "ARMHS_PROVIDER",
            "COUNSELOR_LICENSED",
            "FIRST_AID",
            "CPR",
            "BACKGROUND_CHECK",
            "OTHER",
        ],
    )
    _create_enum(
        "qualification_status",
        [
            "ACTIVE",
            "EXPIRED",
            "PENDING_VERIFICATION",
            "REVOKED",
        ],
    )

    # ---- Trigger function: set_updated_at ----
    op.execute(
        """
        CREATE OR REPLACE FUNCTION set_updated_at()
        RETURNS TRIGGER AS $$
        BEGIN
            NEW.updated_at = now();
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """,
    )

    # ---- Agencies + programs ----
    op.create_table(
        "agencies",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column(
            "status",
            postgresql.ENUM(name="agency_status", create_type=False),
            nullable=False,
            server_default="ACTIVE",
        ),
        sa.Column("timezone", sa.String(64), nullable=False, server_default="America/Chicago"),
        sa.Column(
            "settings", postgresql.JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "idx_agencies_status",
        "agencies",
        ["status"],
        postgresql_where=sa.text("deleted_at IS NULL"),
    )
    op.execute(
        "CREATE TRIGGER trg_agencies_updated_at "
        "BEFORE UPDATE ON agencies "
        "FOR EACH ROW EXECUTE FUNCTION set_updated_at()",
    )

    op.create_table(
        "programs",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "code",
            postgresql.ENUM(name="program_type", create_type=False),
            unique=True,
            nullable=False,
        ),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("description", sa.Text, nullable=True),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default=sa.text("true")),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.execute(
        "CREATE TRIGGER trg_programs_updated_at "
        "BEFORE UPDATE ON programs "
        "FOR EACH ROW EXECUTE FUNCTION set_updated_at()",
    )

    # ---- Seed programs ----
    programs_table = sa.table(
        "programs",
        sa.column("code", postgresql.ENUM(name="program_type", create_type=False)),
        sa.column("name", sa.String),
        sa.column("description", sa.Text),
    )
    op.bulk_insert(
        programs_table,
        [
            {
                "code": "PCA",
                "name": "Personal Care Assistance",
                "description": "Waiver-funded PCA services",
            },
            {
                "code": "CFSS",
                "name": "Community First Services & Supports",
                "description": "CFSS replacement for PCA",
            },
            {
                "code": "245D",
                "name": "245D Licensed Services",
                "description": "Minnesota 245D home & community-based services",
            },
            {
                "code": "ARMHS",
                "name": "Adult Rehabilitative Mental Health Services",
                "description": "Mental health rehabilitation",
            },
            {
                "code": "COUNSELING",
                "name": "Counseling Services",
                "description": "Mental health & substance use counseling",
            },
        ],
    )

    op.create_table(
        "agency_programs",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "agency_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("agencies.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "program_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("programs.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("is_enabled", sa.Boolean, nullable=False, server_default=sa.text("true")),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint("agency_id", "program_id", name="uq_agency_program"),
    )
    op.create_index("idx_agency_programs_agency_id", "agency_programs", ["agency_id"])
    op.execute(
        "CREATE TRIGGER trg_agency_programs_updated_at "
        "BEFORE UPDATE ON agency_programs "
        "FOR EACH ROW EXECUTE FUNCTION set_updated_at()",
    )

    # ---- Identity tables ----
    op.create_table(
        "users",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("email", postgresql.CITEXT, unique=True, nullable=False),
        sa.Column("password_hash", sa.Text, nullable=True),
        sa.Column("full_name", sa.String(255), nullable=False),
        sa.Column("phone", sa.String(32), nullable=True),
        sa.Column(
            "status",
            postgresql.ENUM(name="user_status", create_type=False),
            nullable=False,
            server_default="INVITED",
        ),
        sa.Column("failed_login_attempts", sa.Integer, nullable=False, server_default="0"),
        sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_password_change_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "must_change_password", sa.Boolean, nullable=False, server_default=sa.text("false")
        ),
        # Email verification (ADR-0016)
        sa.Column("email_verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("invitation_token_hash", sa.Text, nullable=True),
        sa.Column("invitation_token_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("invitation_consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "idx_users_status",
        "users",
        ["status"],
        postgresql_where=sa.text("deleted_at IS NULL"),
    )
    op.create_index(
        "idx_users_deleted_at",
        "users",
        ["deleted_at"],
        postgresql_where=sa.text("deleted_at IS NOT NULL"),
    )
    op.create_index(
        "idx_users_email_verified",
        "users",
        ["email_verified_at"],
        postgresql_where=sa.text("deleted_at IS NULL"),
    )
    op.execute(
        "CREATE TRIGGER trg_users_updated_at "
        "BEFORE UPDATE ON users "
        "FOR EACH ROW EXECUTE FUNCTION set_updated_at()",
    )

    op.create_table(
        "user_roles",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("role", postgresql.ENUM(name="user_role", create_type=False), nullable=False),
        sa.Column(
            "agency_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("agencies.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "(role = 'SUPER_ADMIN' AND agency_id IS NULL) OR (role <> 'SUPER_ADMIN')",
            name="ck_super_admin_no_agency",
        ),
        sa.UniqueConstraint("user_id", "role", "agency_id", name="uq_user_role_per_agency"),
    )
    op.create_index("idx_user_roles_user_id", "user_roles", ["user_id"])
    op.create_index(
        "idx_user_roles_agency_id",
        "user_roles",
        ["agency_id"],
        postgresql_where=sa.text("agency_id IS NOT NULL"),
    )
    op.execute(
        "CREATE TRIGGER trg_user_roles_updated_at "
        "BEFORE UPDATE ON user_roles "
        "FOR EACH ROW EXECUTE FUNCTION set_updated_at()",
    )

    op.create_table(
        "email_verification_otps",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("email", postgresql.CITEXT, nullable=False),
        sa.Column("otp_hash", sa.Text, nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("attempts", sa.Integer, nullable=False, server_default="0"),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ip_address", postgresql.INET, nullable=True),
        sa.Column("user_agent", sa.Text, nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index(
        "idx_otp_user_active",
        "email_verification_otps",
        ["user_id", "expires_at"],
        postgresql_where=sa.text("consumed_at IS NULL"),
    )
    op.create_index("idx_otp_email_recent", "email_verification_otps", ["email", "created_at"])

    op.create_table(
        "auth_audit_events",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "event_type",
            postgresql.ENUM(name="auth_audit_event_type", create_type=False),
            nullable=False,
        ),
        sa.Column(
            "event_metadata",
            postgresql.JSONB,
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("ip_address", postgresql.INET, nullable=True),
        sa.Column("user_agent", sa.Text, nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index("idx_auth_audit_user", "auth_audit_events", ["user_id", "created_at"])
    op.create_index("idx_auth_audit_type_recent", "auth_audit_events", ["event_type", "created_at"])

    # Append-only enforcement: prevent UPDATE and DELETE on auth_audit_events.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION reject_modification()
        RETURNS TRIGGER AS $$
        BEGIN
            RAISE EXCEPTION 'auth_audit_events is append-only; % is not allowed', TG_OP
                USING ERRCODE = 'restrict_violation';
        END;
        $$ LANGUAGE plpgsql;
        """,
    )
    op.execute(
        "CREATE TRIGGER trg_auth_audit_no_update "
        "BEFORE UPDATE ON auth_audit_events "
        "FOR EACH ROW EXECUTE FUNCTION reject_modification()",
    )
    op.execute(
        "CREATE TRIGGER trg_auth_audit_no_delete "
        "BEFORE DELETE ON auth_audit_events "
        "FOR EACH ROW EXECUTE FUNCTION reject_modification()",
    )


# --------------------------------------------------------------------------
# Downgrade
# --------------------------------------------------------------------------
def downgrade() -> None:
    # Drop in reverse-dependency order. Triggers drop with their tables.

    # Drop auth_audit_events trigger functions first (they live independently
    # of the tables; explicit drop keeps downgrade idempotent).
    op.execute("DROP TRIGGER IF EXISTS trg_auth_audit_no_update ON auth_audit_events")
    op.execute("DROP TRIGGER IF EXISTS trg_auth_audit_no_delete ON auth_audit_events")
    op.execute("DROP FUNCTION IF EXISTS reject_modification()")

    op.drop_table("auth_audit_events")
    op.drop_table("email_verification_otps")
    op.drop_table("user_roles")
    op.drop_table("users")
    op.drop_table("agency_programs")
    op.drop_table("programs")
    op.drop_table("agencies")

    op.execute("DROP FUNCTION IF EXISTS set_updated_at()")

    # Drop enums in reverse order. CASCADE handles dependencies between
    # enum-typed columns; we drop them all anyway.
    for name in reversed(list(ENUM_TYPE_NAMES.values())):
        _drop_enum(name)

    # Don't drop extensions — they're shared with other databases. If
    # you're truly resetting, drop them manually.
