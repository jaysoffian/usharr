"""Sonarr library polling — cache title slugs for deep-linking.

Deep-link route:
    {base}/series/{titleSlug}
"""

import logging

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from usharr import queries
from usharr.arr import get_arr
from usharr.config import get_config

logger = logging.getLogger(__name__)


class Model(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")


class Series(Model):
    id: int
    title_slug: str = Field(default="", alias="titleSlug")
    path: str = ""  # series folder


SERIES_ADAPTER: TypeAdapter[list[Series]] = TypeAdapter(list[Series])


async def sync() -> None:
    try:
        cfg = get_config().sonarr
        if not cfg.url or not cfg.api_key:
            logger.info("sonarr_sync: not configured; skipping")
            return

        series = await get_arr(SERIES_ADAPTER, cfg.url, cfg.api_key, "series")

        seen: set[int] = set()
        for item in series:
            seen.add(item.id)
            await queries.upsert_sonarr_series(
                series_id=item.id,
                title_slug=item.title_slug or None,
                remote_path=item.path or None,
                path_map=cfg.path_map,
            )

        stale = sorted(await queries.list_sonarr_series_ids() - seen)
        removed = await queries.delete_sonarr_series(stale) if stale else 0
        logger.info("sonarr_sync: series=%d removed=%d", len(series), removed)
    except Exception:
        logger.exception("sonarr_sync errored")
