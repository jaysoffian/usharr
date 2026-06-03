"""Oxyde connection lifecycle: open the pool, apply migrations.

Per Oxyde's SQLite guidance, a single connection is enough (connections.md:
"SQLite: single connection is usually enough") — it sidesteps write-lock
contention. WAL/synchronous/busy_timeout come from PoolSettings defaults and
sqlx enables ``PRAGMA foreign_keys`` per connection, so FK CASCADEs are enforced.
The async win is running queries off the event loop, not connection parallelism.
"""

import logging
import os
from pathlib import Path

from oxyde import db
from oxyde.db import PoolSettings
from oxyde.migrations import apply_migrations

logger = logging.getLogger(__name__)

DB_PATH = Path(os.environ.get("USHARR_DB", "/config/usharr.db"))
MIGRATIONS_DIR = Path(__file__).parent / "migrations"


async def connect() -> None:
    """Open the pool and migrate to the latest schema.

    In USHARR_DB_RO mode (a copied prod DB for `make dev`) migrations are
    skipped so the snapshot's schema is left untouched.
    """
    read_only = bool(os.environ.get("USHARR_DB_RO"))
    if not read_only:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    # SQLite: one connection is enough (Oxyde guidance) — avoids write-lock
    # contention. WAL/busy_timeout/foreign_keys come from the driver defaults.
    await db.init(
        default=f"sqlite://{DB_PATH}", settings=PoolSettings(max_connections=1)
    )
    if read_only:
        logger.info("Opened DB at %s (read-only; migrations skipped)", DB_PATH)
        return
    applied = await apply_migrations(
        migrations_dir=str(MIGRATIONS_DIR), db_alias="default"
    )
    if applied:
        logger.info("Applied %d migration(s): %s", len(applied), ", ".join(applied))
    logger.info("Opened DB at %s", DB_PATH)


async def close() -> None:
    await db.close()
