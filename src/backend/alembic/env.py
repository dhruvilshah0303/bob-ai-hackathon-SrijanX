import sys
from logging.config import fileConfig
from pathlib import Path

from sqlalchemy import engine_from_config
from sqlalchemy import pool

from alembic import context

# So `import db` / `import models` resolve the same way they do when
# uvicorn runs from backend/ directly (alembic is invoked from the same
# directory, but be explicit rather than relying on cwd).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db  # noqa: E402 - uses db._resolve_database_url(), same URL app.py connects to
import models  # noqa: E402

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config

# Interpret the config file for Python logging.
# This line sets up loggers basically.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Autogenerate support: only models.Base's tables (users, hospitals, ...).
# db.py's own Core tables (audit_log, trips) are intentionally NOT part of
# this metadata - they're a separate, already-working persistence path (see
# models.py's module docstring) that isn't migrated through Alembic.
target_metadata = models.Base.metadata

# Always use the same DATABASE_URL resolution app.py/db.py use (env var,
# normalized postgres:// -> postgresql://, sqlite fallback) instead of a
# separate hardcoded value in alembic.ini - one source of truth for which
# database gets migrated.
config.set_main_option("sqlalchemy.url", db.DATABASE_URL)

# db.py owns these two tables directly (SQLAlchemy Core, no Alembic) - tell
# autogenerate to ignore them entirely so it never proposes dropping them
# just because they aren't part of models.Base.metadata.
_LEGACY_CORE_TABLES = {"audit_log", "trips"}


def include_object(object, name, type_, reflected, compare_to):
    if type_ == "table" and name in _LEGACY_CORE_TABLES:
        return False
    return True

# other values from the config, defined by the needs of env.py,
# can be acquired:
# my_important_option = config.get_main_option("my_important_option")
# ... etc.


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode.

    This configures the context with just a URL
    and not an Engine, though an Engine is acceptable
    here as well.  By skipping the Engine creation
    we don't even need a DBAPI to be available.

    Calls to context.execute() here emit the given string to the
    script output.

    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        include_object=include_object,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode.

    In this scenario we need to create an Engine
    and associate a connection with the context.

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
            include_object=include_object,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
