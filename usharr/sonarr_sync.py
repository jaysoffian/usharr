"""Sonarr library polling — cache title slugs for deep-linking.

Deep-link route:
    {base}/series/{titleSlug}
"""

import logging

import httpx
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from usharr import db
from usharr.config import get_config

logger = logging.getLogger(__name__)


class Model(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")


class Series(Model):
    id: int
    title_slug: str = Field(default="", alias="titleSlug")
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


async def sync() -> None:
    try:
        s = get_config().sonarr
        if not s.url or not s.api_key:
            logger.info("sonarr_sync: not configured; skipping")
            return

        series = await get_series(s.url, s.api_key)

        seen: set[int] = set()
        for row in series:
            seen.add(row.id)
            db.upsert_sonarr_series(
                series_id=row.id,
                title_slug=row.title_slug or None,
                remote_path=row.path or None,
                path_map=s.path_map,
            )

        stale = sorted(db.list_sonarr_series_ids() - seen)
        removed = db.delete_sonarr_series(stale) if stale else 0
        logger.info("sonarr_sync: series=%d removed=%d", len(series), removed)
    except Exception:
        logger.exception("sonarr_sync errored")
