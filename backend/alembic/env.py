"""Alembic migration environment.

This module is invoked by Alembic to (a) discover the target metadata for
autogenerate and (b) supply the database URL. Both come from our application
settings — never from `alembic.ini` — so we have a single source of truth and
no risk of committing credentials.

Adding a new entity:
    1. Create `app/models/<entity>.py` (subclass `Base`).
    2. Import it in `app/models/__init__.py` so its metadata is registered.
    3. Run `alembic revision --autogenerate -m "add <entity>"` and review the
       generated diff before committing.
"""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context

# Importing `app.models` registers every entity's metadata against `Base`.
# Alembic's autogenerate walks `target_metadata` to detect schema drift, so this
# single import line is what makes new models discoverable.
from app import models  # noqa: F401 — re-export site for entity registration
from app.core.config import get_settings
from app.models.base_class import Base
from sqlalchemy import engine_from_config, pool

# --- Alembic config object ----------------------------------------------------
config = context.config

# Inject the runtime DB URL so the static .ini file doesn't need credentials.
settings = get_settings()
config.set_main_option("sqlalchemy.url", settings.database_url_str)

# Enable Python logging based on alembic.ini's [loggers] section.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Target metadata for `--autogenerate` diffing.
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode (emits SQL to stdout, no DB connection).

    Useful for generating SQL scripts to hand off to a DBA.
    """
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,  # detect column type changes
        compare_server_default=True,  # detect default-value changes
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode against the configured database."""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,  # one-shot connection, don't pollute pool
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            compare_server_default=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
