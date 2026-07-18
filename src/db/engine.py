"""Async engine + session factory + schema bootstrap for SQLAlchemy.

Schema management has three cases the lifespan needs to handle correctly:

  1. **Fresh DB** (empty file or :memory:) — create_all to materialize the
     current model state, then stamp head so alembic knows we're up to
     date for future migrations.
  2. **Legacy DB** (tables exist but no `alembic_version` table — i.e. the
     Phase A `create_all`-only era) — run `alembic upgrade head` against
     it; the first migration's `down_revision = None` makes alembic
     happy to apply on top of a pre-existing schema.
  3. **Alembic-managed DB** (`alembic_version` table present) — run
     `alembic upgrade head` to apply any pending migrations.

The detection happens via SQLAlchemy's `Inspector`. Tests use the
in-memory engine and hit case 1.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from alembic import command
from alembic.config import Config as AlembicConfig
from sqlalchemy import inspect
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from src.db.models import Base

log = logging.getLogger("llopster.db")

_ALEMBIC_INI = Path(__file__).resolve().parents[2] / "alembic.ini"


def create_engine(database_url: str) -> AsyncEngine:
    """Build an async engine.

    For SQLite file URLs, ensure the parent directory exists so the first run
    on a fresh volume doesn't crash.  For PostgreSQL URLs the driver is
    asyncpg (``postgresql+asyncpg://...``); pass through with no filesystem
    setup needed.
    """
    if database_url.startswith("sqlite"):
        prefix = "sqlite+aiosqlite:///"
        if database_url.startswith(prefix):
            raw_path = database_url[len(prefix):]
            file_path = Path(raw_path)
            file_path.parent.mkdir(parents=True, exist_ok=True)
    return create_async_engine(database_url, future=True)


def get_sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


def _sync_url(database_url: str) -> str:
    """Return the URL for Alembic.

    alembic/env.py uses the async engine directly (asyncpg / aiosqlite), so
    the URL is passed through unchanged — no psycopg2 conversion needed.
    """
    return database_url


def _apply_alembic(database_url: str, action: str) -> None:
    """Run `alembic upgrade head` or `alembic stamp head` against the URL."""
    if not _ALEMBIC_INI.exists():
        log.warning("alembic.ini not found at %s — skipping %s", _ALEMBIC_INI, action)
        return
    cfg = AlembicConfig(str(_ALEMBIC_INI))
    cfg.set_main_option("sqlalchemy.url", _sync_url(database_url))
    if action == "upgrade":
        command.upgrade(cfg, "head")
    elif action == "stamp":
        command.stamp(cfg, "head")
    else:
        raise ValueError(f"unknown alembic action: {action}")


async def init_schema(engine: AsyncEngine, *, migrate: bool = True) -> None:
    """Bring the DB schema to the current model state.

    With ``migrate=True`` (default — agent process):

      Detects which of the three cases (fresh / legacy / alembic-managed) the
      DB is in and applies the right combination of ``create_all`` + alembic
      upgrade/stamp.

    With ``migrate=False`` (dashboard process):

      Only ensures tables exist (``create_all`` is idempotent, uses
      ``CREATE TABLE IF NOT EXISTS``). Skips alembic stamp/upgrade entirely.
      This prevents the race condition where two pods coming up against a
      fresh PostgreSQL would both try to insert the initial ``alembic_version``
      row — only the agent owns migrations. If the agent hasn't started yet,
      the dashboard will see empty tables (graceful — empty list of runs).
    """
    def _detect(sync_conn) -> tuple[bool, bool]:
        insp = inspect(sync_conn)
        existing = set(insp.get_table_names())
        return ("alembic_version" in existing, "runs" in existing)

    async with engine.begin() as conn:
        has_alembic_version, has_runs = await conn.run_sync(_detect)

    url = str(engine.url.render_as_string(hide_password=False))

    if not migrate:
        # Dashboard mode: idempotent table creation only, no alembic.
        if not has_runs:
            log.info("dashboard mode: ensuring tables exist via create_all")
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
        else:
            log.info("dashboard mode: tables present, skipping schema work")
        return

    if has_alembic_version:
        # Case 3: alembic-managed. Apply any pending migrations.
        log.info("schema is alembic-managed; running upgrade head")
        await asyncio.to_thread(_apply_alembic, url, "upgrade")
    elif has_runs:
        # Case 2: legacy pre-alembic DB. Apply the migration chain to bring
        # the existing schema up to head.
        log.info("legacy schema detected (no alembic_version); running upgrade head")
        await asyncio.to_thread(_apply_alembic, url, "upgrade")
    else:
        # Case 1: fresh DB. create_all gets us to the latest model state in
        # one shot; stamp head tells alembic this is the current revision.
        log.info("fresh schema; running create_all + stamp head")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        # Skip stamping for in-memory DBs (tests) — they don't persist and
        # alembic_version isn't useful there.
        if ":memory:" not in url:
            await asyncio.to_thread(_apply_alembic, url, "stamp")

    log.info("schema initialized at %s", engine.url)
