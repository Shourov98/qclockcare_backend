"""Declarative base + reusable mixins.

`Base` is the SQLAlchemy `DeclarativeBase` used by all ORM models. The naming
convention is critical — Alembic autogenerate uses it to generate constraint
names that match what Postgres expects.

Mixins:
- `IdMixin`        — UUID primary key
- `TimestampedMixin` — created_at + updated_at
- `SoftDeleteMixin`  — deleted_at for soft delete

Stack them: `class User(IdMixin, TimestampedMixin, SoftDeleteMixin, Base): ...`
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import ClassVar

from sqlalchemy import DateTime, MetaData, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from src.shared.utils.datetime_utils import utc_now

NAMING_CONVENTION: dict[str, str] = {
    "ix": "ix_%(table_name)s_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """All ORM models inherit from this.

    The naming convention ensures Alembic-generated constraints have stable
    names across runs.
    """

    metadata = MetaData(naming_convention=NAMING_CONVENTION)

    # Type-annotated column defaults — used by all Mapped[...] columns.
    # `ClassVar` declares this as a class-level config, not a column.
    type_annotation_map: ClassVar[dict[type, type]] = {
        dict: dict,
        list: list,
    }


# --------------------------------------------------------------------------
# Mixins
# --------------------------------------------------------------------------
class IdMixin:
    """UUID primary key, server-generated."""

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=func.gen_random_uuid(),
    )


class TimestampedMixin:
    """created_at + updated_at, both UTC, with auto-update on updated_at.

    The trigger that actually updates `updated_at` lives in the migration
    (`set_updated_at` function). We also set `onupdate=utc_now` as a Python
    safety net for code paths that bypass triggers.
    """

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        server_default=func.now(),
    )

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        onupdate=utc_now,
        server_default=func.now(),
    )


class SoftDeleteMixin:
    """deleted_at — NULL means active; non-NULL means soft-deleted.

    Soft-deleted rows are still in the table but filtered out of queries
    by convention. Repositories enforce `WHERE deleted_at IS NULL`.
    """

    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        default=None,
    )

    @property
    def is_deleted(self) -> bool:
        return self.deleted_at is not None
