"""Radarr library polling — cache TMDB IDs for deep-linking.

Deep-link route:
    {base}/movie/{tmdbId}
"""

import logging
import time

import httpx
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from usharr import db
from usharr.config import load_config

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


async def get_movies(base: str, api_key: str) -> list[Movie]:
    url = f"{base.rstrip('/')}/api/v3/movie"
    async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
        r = await client.get(url, headers={"X-Api-Key": api_key})
    if r.status_code != 200:
        msg = f"GET {url} → {r.status_code}"
        raise RuntimeError(msg)
    try:
        return MOVIES_ADAPTER.validate_json(r.content)
    except ValidationError as exc:
        msg = f"bad radarr response: {exc}"
        raise RuntimeError(msg) from exc


async def sync() -> None:
    try:
        r = load_config().radarr
        if not r.url or not r.api_key:
            logger.info("radarr_sync: not configured; skipping")
            return

        movies = await get_movies(r.url, r.api_key)
        now = int(time.time())

        seen: set[int] = set()
        for m in movies:
            seen.add(m.id)
            file_path = m.movie_file.path if m.movie_file else None
            db.upsert_radarr_movie(
                movie_id=m.id,
                tmdb_id=m.tmdb_id,
                remote_path=file_path or None,
                path_map=r.path_map,
                updated_at=now,
            )

        stale = sorted(db.list_radarr_movie_ids() - seen)
        removed = db.delete_radarr_movies(stale) if stale else 0
        logger.info("radarr_sync: movies=%d removed=%d", len(movies), removed)
    except Exception:
        logger.exception("radarr_sync errored")
