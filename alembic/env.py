"""Alembic migration environment — async (asyncpg / aiosqlite) mode.

We use the async engine pattern so we never need psycopg2 as a separate
sync driver; the same asyncpg dependency used by the app handles both.

Intentionally does NOT call `logging.config.fileConfig(config.config_file_name)`
— the default alembic.ini [loggers] sections would reset the root logger to
WARNING and wipe out the application's basicConfig.  Our app configures logging
in src/api/main.py; alembic just inherits it.
"""

from __future__ import annotations

import asyncio

from sqlalchemy.ext.asyncio import create_async_engine

from alembic import context

from src.db.models import Base  # noqa: E402

config = context.config
target_metadata = Base.metadata


# ---------------------------------------------------------------------------
# Offline mode — emit raw SQL without a live connection.
# ---------------------------------------------------------------------------

def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


# ---------------------------------------------------------------------------
# Online mode — async engine (asyncpg or aiosqlite).
# ---------------------------------------------------------------------------

def do_run_migrations(connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    url = config.get_main_option("sqlalchemy.url")
    connectable = create_async_engine(url, future=True)
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


# ---------------------------------------------------------------------------

if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
