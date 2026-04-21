"""Radarr library polling — cache TMDB IDs for deep-linking.

Deep-link route:
    {base}/movie/{tmdbId}
"""

import asyncio
import logging
import time

import httpx
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from usharr import db, plex
from usharr.config import INTERVAL_SECONDS, Config

logger = logging.getLogger(__name__)


class _Model(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")


class _MovieFile(_Model):
    path: str = ""


class _Movie(_Model):
    id: int
    tmdb_id: int | None = Field(default=None, alias="tmdbId")
    title: str = ""
    year: int | None = None
    has_file: bool = Field(default=False, alias="hasFile")
    movie_file: _MovieFile | None = Field(default=None, alias="movieFile")


_MOVIES_ADAPTER: TypeAdapter[list[_Movie]] = TypeAdapter(list[_Movie])


async def _get_movies(base: str, api_key: str) -> list[_Movie]:
    url = f"{base.rstrip('/')}/api/v3/movie?apikey={api_key}"
    async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
        r = await client.get(url, headers={"X-Api-Key": api_key})
    if r.status_code != 200:
        msg = f"GET {url.replace(api_key, '***')} → {r.status_code}"
        raise RuntimeError(msg)
    try:
        return _MOVIES_ADAPTER.validate_json(r.content)
    except ValidationError as exc:
        msg = f"bad radarr response: {exc}"
        raise RuntimeError(msg) from exc


async def sync_once(config: Config) -> dict:
    r = config.radarr
    if not r.url or not r.api_key:
        logger.info("radarr_sync: not configured; skipping")
        return {"skipped": True}

    try:
        movies = await _get_movies(r.url, r.api_key)
    except Exception as exc:
        logger.warning("radarr_sync: fetch failed: %s", exc)
        return {"error": str(exc)}

    file_paths = db.list_paths()
    now = int(time.time())

    seen: set[int] = set()
    for m in movies:
        seen.add(m.id)
        file_path = m.movie_file.path if m.movie_file else None
        mapped = plex.apply_path_map(file_path, r.path_map) if file_path else None
        local = plex.match_local_path(mapped, file_paths) if mapped else None
        db.upsert_radarr_movie(
            movie_id=m.id,
            tmdb_id=m.tmdb_id,
            title=m.title or None,
            year=m.year,
            path=file_path or None,
            local_path=local,
            updated_at=now,
        )

    stale = sorted(db.list_radarr_movie_ids() - seen)
    removed = db.delete_radarr_movies(stale) if stale else 0
    summary = {"movies": len(movies), "removed": removed}
    logger.info("radarr_sync: %s", summary)
    return summary


async def radarr_sync_loop(config: Config) -> None:
    while True:
        try:
            await sync_once(config)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("radarr_sync errored")
        try:
            await asyncio.sleep(INTERVAL_SECONDS)
        except asyncio.CancelledError:
            raise
