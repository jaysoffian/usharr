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
from oxyde.queries.raw import execute_raw

from usharr.models import PlexAuth

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


# Columns shared by the legacy and current ardetector tables. The legacy table
# was keyed by video_path; the current one adds a surrogate id, so we name
# columns explicitly and let id auto-assign.
ARDETECTOR_COLS = (
    "video_path",
    "error",
    "aspect_primary",
    "aspect_widest",
    "aspect_samples",
    "color_pct",
)


async def import_legacy_ardetector(old_db: Path | str) -> int:
    """Copy ardetector rows from a pre-Oxyde usharr.db into the current DB.

    Carries over the slow aspect-detection results (a ~day to re-acquire) for
    files that still exist (``video_path`` present in ``video_file``) and aren't
    already probed. Run after a scan has repopulated ``video_file``. Returns the
    number of rows imported.
    """
    cols = ", ".join(ARDETECTOR_COLS)
    before = await _count("ardetector")
    await execute_raw(f"ATTACH DATABASE '{old_db}' AS legacy")
    try:
        await execute_raw(
            f"INSERT INTO ardetector ({cols})"
            f" SELECT {cols} FROM legacy.ardetector"
            f" WHERE video_path IN (SELECT path FROM video_file)"
            f"   AND video_path NOT IN (SELECT video_path FROM ardetector)"
        )
    finally:
        await execute_raw("DETACH DATABASE legacy")
    imported = await _count("ardetector") - before
    logger.info("Imported %d ardetector row(s) from %s", imported, old_db)
    return imported


async def _count(table: str) -> int:
    rows = await execute_raw(f"SELECT COUNT(*) AS n FROM {table}")
    return rows[0]["n"]


# Legacy kv keys → PlexAuth fields.
PLEX_AUTH_KEYS = {
    "plex_client_id": "client_id",
    "plex_token": "token",
    "plex_server_url": "server_url",
    "plex_server_name": "server_name",
    "plex_machine_id": "machine_id",
}


async def import_legacy_plex_auth(old_db: Path | str) -> bool:
    """Carry the Plex link from a legacy usharr.db's kv table into plex_auth.

    Returns True if a token was imported. No-op when already linked or the
    legacy DB has no stored token.
    """
    existing = await PlexAuth.objects.get_or_none(id=1)
    if existing and existing.token:
        return False
    keys = "', '".join(PLEX_AUTH_KEYS)
    await execute_raw(f"ATTACH DATABASE '{old_db}' AS legacy")
    try:
        rows = await execute_raw(
            f"SELECT key, value FROM legacy.kv WHERE key IN ('{keys}')"
        )
    finally:
        await execute_raw("DETACH DATABASE legacy")
    fields = {
        PLEX_AUTH_KEYS[r["key"]]: r["value"] for r in rows if r["value"] is not None
    }
    if not fields.get("token"):
        return False
    await PlexAuth.objects.create(
        id=1,
        client_id=fields.get("client_id"),
        token=fields.get("token"),
        server_url=fields.get("server_url"),
        server_name=fields.get("server_name"),
        machine_id=fields.get("machine_id"),
    )
    logger.info("Imported Plex link for %s", fields.get("server_name") or "server")
    return True
