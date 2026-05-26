"""Bazarr library polling — cache movie/series IDs for deep-linking.

Bazarr's deep-link routes (v1 UI):

    /movies/{radarrId}              — movie subtitle management
    /episodes/{sonarrSeriesId}      — per-series episode subtitle management

Both IDs come from Bazarr's API responses (`/api/movies`, `/api/series`).
We upsert a local `bazarr_movie` / `bazarr_series` row per item with the
folder path Bazarr reports, resolved to a local folder via longest-suffix
matching so deep-link lookup at render time is one indexed query.
"""

import logging
import time

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from usharr import db
from usharr.config import load_config

logger = logging.getLogger(__name__)


class Model(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")


class Movie(Model):
    radarr_id: int = Field(alias="radarrId")
    path: str = ""


class Series(Model):
    sonarr_series_id: int = Field(alias="sonarrSeriesId")
    path: str = ""


class MoviesResp(Model):
    data: list[Movie] = Field(default_factory=list)


class SeriesResp(Model):
    data: list[Series] = Field(default_factory=list)


async def get[T: Model](model: type[T], url: str, api_key: str) -> T:
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.get(url, headers={"X-API-KEY": api_key})
    if r.status_code != 200:
        msg = f"GET {url} → {r.status_code}"
        raise RuntimeError(msg)
    try:
        return model.model_validate_json(r.content)
    except ValidationError as exc:
        msg = f"bad response at {url}: {exc}"
        raise RuntimeError(msg) from exc


async def sync() -> None:
    try:
        bz = load_config().bazarr
        if not bz.url or not bz.api_key:
            logger.info("bazarr_sync: not configured; skipping")
            return

        base_url = bz.url.rstrip("/")
        movies_resp = await get(
            MoviesResp,
            f"{base_url}/api/movies?start=0&length=-1",
            bz.api_key,
        )
        series_resp = await get(
            SeriesResp,
            f"{base_url}/api/series?start=0&length=-1",
            bz.api_key,
        )

        now = int(time.time())

        seen_movies: set[int] = set()
        for m in movies_resp.data:
            seen_movies.add(m.radarr_id)
            db.upsert_bazarr_movie(
                radarr_id=m.radarr_id,
                remote_path=m.path or None,
                path_map=bz.path_map,
                updated_at=now,
            )

        seen_series: set[int] = set()
        for s in series_resp.data:
            seen_series.add(s.sonarr_series_id)
            db.upsert_bazarr_series(
                sonarr_id=s.sonarr_series_id,
                remote_path=s.path or None,
                path_map=bz.path_map,
                updated_at=now,
            )

        stale_movies = sorted(db.list_bazarr_movie_ids() - seen_movies)
        stale_series = sorted(db.list_bazarr_series_ids() - seen_series)
        db.delete_bazarr_movies(stale_movies)
        db.delete_bazarr_series(stale_series)

        summary = {
            "movies": len(movies_resp.data),
            "series": len(series_resp.data),
            "removed_movies": len(stale_movies),
            "removed_series": len(stale_series),
        }
        logger.info("bazarr_sync: %s", summary)
    except Exception:
        logger.exception("bazarr_sync errored")
