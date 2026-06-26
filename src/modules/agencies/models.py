"""Agencies module — agency and program ORM models.

Tables:
- `agencies`       — agency tenants
- `programs`       — master reference list (seeded)
- `agency_programs` — which programs each agency offers

See `13_DATABASE_SCHEMA_COMPLETE.md` §5.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    Boolean,
    Enum,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.shared.domain.base_entity import Base, IdMixin, SoftDeleteMixin, TimestampedMixin
from src.shared.domain.enum_mapping import pg_name
from src.shared.domain.enums import AgencyStatus, ProgramType

if TYPE_CHECKING:
    from src.modules.identity.models import UserRoleAssignment


# --------------------------------------------------------------------------
# agencies
# --------------------------------------------------------------------------
class Agency(IdMixin, TimestampedMixin, SoftDeleteMixin, Base):
    """A care agency tenant. Every domain table that is agency-scoped has
    `agency_id` FK to this table. RLS policies on those tables read
    `current_setting('app.current_agency_id')` to filter rows.
    """

    __tablename__ = "agencies"

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[AgencyStatus] = mapped_column(
        Enum(AgencyStatus, name=pg_name(AgencyStatus)),
        nullable=False,
        default=AgencyStatus.ACTIVE,
        server_default=AgencyStatus.ACTIVE.value,
    )
    timezone: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        default="America/Chicago",
        server_default="America/Chicago",
    )
    settings: Mapped[dict[str, Any]] = mapped_column(
        JSONB(), nullable=False, default=dict, server_default="{}"
    )

    # Relationships
    user_roles: Mapped[list[UserRoleAssignment]] = relationship(
        back_populates="agency",
    )
    agency_programs: Mapped[list[AgencyProgram]] = relationship(
        back_populates="agency",
        cascade="all, delete-orphan",
    )

    __table_args__ = (
        Index(
            "idx_agencies_status",
            "status",
            postgresql_where=text("deleted_at IS NULL"),
        ),
    )


# --------------------------------------------------------------------------
# programs (master reference, seeded)
# --------------------------------------------------------------------------
class Program(IdMixin, TimestampedMixin, Base):
    """Master list of supported program types. Seeded once in migration 1.

    An `Agency` "offers" a subset of these via `agency_programs`.
    """

    __tablename__ = "programs"

    code: Mapped[ProgramType] = mapped_column(
        Enum(ProgramType, name=pg_name(ProgramType)),
        unique=True,
        nullable=False,
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )

    agency_programs: Mapped[list[AgencyProgram]] = relationship(
        back_populates="program",
    )


# --------------------------------------------------------------------------
# agency_programs (junction)
# --------------------------------------------------------------------------
class AgencyProgram(IdMixin, TimestampedMixin, Base):
    """Which programs each agency offers.

    An agency can offer a given program at most once. The unique constraint
    on `(agency_id, program_id)` enforces that.
    """

    __tablename__ = "agency_programs"

    agency_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("agencies.id", ondelete="CASCADE"),
        nullable=False,
    )
    program_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("programs.id", ondelete="RESTRICT"),
        nullable=False,
    )
    is_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )

    agency: Mapped[Agency] = relationship(back_populates="agency_programs")
    program: Mapped[Program] = relationship(back_populates="agency_programs")

    __table_args__ = (
        UniqueConstraint("agency_id", "program_id", name="uq_agency_program"),
        Index("idx_agency_programs_agency_id", "agency_id"),
    )


__all__ = ["Agency", "AgencyProgram", "Program"]
