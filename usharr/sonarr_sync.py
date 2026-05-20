"""Sonarr library polling — cache title slugs for deep-linking.

Deep-link route:
    {base}/series/{titleSlug}
"""

import asyncio
import logging
import time

import httpx
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from usharr import db
from usharr.config import INTERVAL_SECONDS, Config

logger = logging.getLogger(__name__)


class Model(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")


class Series(Model):
    id: int
    tvdb_id: int | None = Field(default=None, alias="tvdbId")
    title_slug: str = Field(default="", alias="titleSlug")
    title: str = ""
    path: str = ""  # series folder


SERIES_ADAPTER: TypeAdapter[list[Series]] = TypeAdapter(list[Series])


async def get_series(base: str, api_key: str) -> list[Series]:
    url = f"{base.rstrip('/')}/api/v3/series"
    async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
        r = await client.get(url, headers={"X-Api-Key": api_key})
    if r.status_code != 200:
        msg = f"GET {url} → {r.status_code}"
        raise RuntimeError(msg)
    try:
        return SERIES_ADAPTER.validate_json(r.content)
    except ValidationError as exc:
        msg = f"bad sonarr response: {exc}"
        raise RuntimeError(msg) from exc


async def sync_once(config: Config) -> dict:
    s = config.sonarr
    if not s.url or not s.api_key:
        logger.info("sonarr_sync: not configured; skipping")
        return {"skipped": True}

    try:
        series = await get_series(s.url, s.api_key)
    except Exception as exc:
        logger.warning("sonarr_sync: fetch failed: %s", exc)
        return {"error": str(exc)}

    now = int(time.time())

    seen: set[int] = set()
    for row in series:
        seen.add(row.id)
        db.upsert_sonarr_series(
            series_id=row.id,
            tvdb_id=row.tvdb_id,
            title_slug=row.title_slug or None,
            title=row.title or None,
            remote_path=row.path or None,
            path_map=s.path_map,
            updated_at=now,
        )

    stale = sorted(db.list_sonarr_series_ids() - seen)
    removed = db.delete_sonarr_series(stale) if stale else 0
    summary = {"series": len(series), "removed": removed}
    logger.info("sonarr_sync: %s", summary)
    return summary


async def sonarr_sync_loop(config: Config) -> None:
    while True:
        try:
            await sync_once(config)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("sonarr_sync errored")
        try:
            await asyncio.sleep(INTERVAL_SECONDS)
        except asyncio.CancelledError:
            raise
