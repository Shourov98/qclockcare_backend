"""Alembic environment.

Pulls the database URL from our `Settings`, sets up SQLAlchemy logging, and
exposes `Base.metadata` (with all ORM models imported) for autogenerate.
"""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

# ---- Load our settings + Base.metadata ----
# Importing these also triggers every model module to register its tables
# on `Base.metadata`, which is what Alembic autogenerate needs.
from src.core.config import settings

# Import every model module so its tables are registered on Base.metadata.
# Keep this list explicit so adding a new module is a deliberate step.
from src.modules.agencies.models import (  # noqa: F401
    Agency,
    AgencyProgram,
    Program,
)
from src.modules.identity.models import (  # noqa: F401
    AuthAuditEvent,
    EmailVerificationOtp,
    RefreshToken,
    SingleUseToken,
    User,
    UserRoleAssignment,
)
from src.shared.domain.base_entity import Base

config = context.config

# Inject the runtime database URL. Use the direct URL (port 5432), NOT
# the pool URL — DDL needs session-level features that pgbouncer in
# transaction mode does not support.
#
# Alembic runs synchronously (no async drivers), so we swap the asyncpg
# prefix for psycopg (v3) which is already in our dev/runtime dependencies.
url = settings.DATABASE_URL.get_secret_value()
if url.startswith("postgresql+asyncpg://"):
    url = "postgresql+psycopg://" + url[len("postgresql+asyncpg://") :]
config.set_main_option("sqlalchemy.url", url)

# Configure stdlib logging from alembic.ini
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# The metadata target for `alembic revision --autogenerate`
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode — emits SQL to stdout/file.

    Useful for `alembic upgrade head --sql` (dry-run output for review).
    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
        version_table="alembic_version",
        version_table_pk_length=128,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations against a live database connection.

    We use a sync engine for migrations (psycopg/psycopg2 driver) — Alembic
    doesn't support async engines out of the box, and DDL is naturally
    synchronous anyway.
    """
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            compare_server_default=True,
            version_table="alembic_version",
            version_table_pk_length=128,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
