"""Radarr library polling — cache TMDB IDs for deep-linking.

Deep-link route:
    {base}/movie/{tmdbId}
"""

import logging

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from usharr import queries
from usharr.arr import get_arr
from usharr.config import get_config

logger = logging.getLogger(__name__)


class Model(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")


class MovieFile(Model):
    path: str = ""


class Movie(Model):
    id: int
    tmdb_id: int | None = Field(default=None, alias="tmdbId")
    has_file: bool = Field(default=False, alias="hasFile")
    movie_file: MovieFile | None = Field(default=None, alias="movieFile")


MOVIES_ADAPTER: TypeAdapter[list[Movie]] = TypeAdapter(list[Movie])


async def sync() -> None:
    try:
        cfg = get_config().radarr
        if not cfg.url or not cfg.api_key:
            logger.info("radarr_sync: not configured; skipping")
            return

        movies = await get_arr(MOVIES_ADAPTER, cfg.url, cfg.api_key, "movie")

        seen: set[int] = set()
        for item in movies:
            seen.add(item.id)
            file_path = item.movie_file.path if item.movie_file else None
            await queries.upsert_radarr_movie(
                movie_id=item.id,
                tmdb_id=item.tmdb_id,
                remote_path=file_path or None,
                path_map=cfg.path_map,
            )

        stale = sorted(await queries.list_radarr_movie_ids() - seen)
        removed = await queries.delete_radarr_movies(stale) if stale else 0
        logger.info("radarr_sync: movies=%d removed=%d", len(movies), removed)
    except Exception:
        logger.exception("radarr_sync errored")
