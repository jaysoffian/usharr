"""Plex synchronization"""

import logging

import httpx
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
)

from usharr import plex, queries
from usharr.config import get_config

logger = logging.getLogger(__name__)


class Model(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")


class Section(Model):
    key: str
    type: str
    title: str = ""


class SectionsContainer(Model):
    directories: list[Section] = Field(default_factory=list, alias="Directory")


class SectionsResponse(Model):
    container: SectionsContainer = Field(alias="MediaContainer")


class LibPart(Model):
    file: str = ""


class LibMedia(Model):
    parts: list[LibPart] = Field(default_factory=list, alias="Part")


class LibMetadata(Model):
    rating_key: str = Field(default="", alias="ratingKey")
    type: str = ""
    title: str = ""
    year: int | None = None
    grandparent_title: str | None = Field(default=None, alias="grandparentTitle")
    parent_index: int | None = Field(default=None, alias="parentIndex")
    index: int | None = None
    media: list[LibMedia] = Field(default_factory=list, alias="Media")

    async def upsert(self, path_map: dict[str, str]) -> None:
        remote_path = next(
            (p.file for m in self.media for p in m.parts if p.file),
            None,
        )
        await queries.upsert_plex_item(
            rating_key=self.rating_key,
            item_type=self.type or "movie",
            title=self.title or None,
            year=self.year,
            show_title=self.grandparent_title,
            season_number=self.parent_index,
            episode_number=self.index,
            remote_path=remote_path,
            path_map=path_map,
        )


class LibContainer(Model):
    metadata: list[LibMetadata] = Field(default_factory=list, alias="Metadata")


class LibResponse(Model):
    container: LibContainer = Field(alias="MediaContainer")


async def get_json[T: Model](model: type[T], url: str) -> T:
    token, _, _ = await plex.load_auth()
    client_id = await plex.get_or_create_client_id()
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.get(url, headers=plex.headers(client_id, token))
    if r.status_code != 200:
        msg = f"GET {url} → {r.status_code}"
        raise plex.PlexError(msg)
    try:
        return model.model_validate_json(r.content)
    except ValidationError as exc:
        msg = f"bad response shape at {url}: {exc}"
        raise plex.PlexError(msg) from exc


async def sync() -> None:
    """Walk sections and upsert every movie + episode into plex_item."""
    try:
        try:
            _, server_url, _ = await plex.load_auth()
        except plex.PlexNotLinkedError:
            logger.info("plex_sync: not linked; skipping")
            return

        server_url = server_url.rstrip("/")
        sections_resp = await get_json(
            SectionsResponse,
            f"{server_url}/library/sections",
        )

        seen: set[str] = set()
        upserted = 0

        path_map = get_config().plex.path_map

        for section in sections_resp.container.directories:
            if section.type == "movie":
                url = f"{server_url}/library/sections/{section.key}/all"
            elif section.type == "show":
                url = f"{server_url}/library/sections/{section.key}/allLeaves"
            else:
                continue
            try:
                resp = await get_json(LibResponse, url)
            except plex.PlexError as exc:
                logger.warning("plex_sync %s: %s", section.title, exc)
                continue
            for item in resp.container.metadata:
                if not item.rating_key:
                    continue
                seen.add(item.rating_key)
                await item.upsert(path_map)
                upserted += 1

        existing = await queries.list_plex_rating_keys()
        stale = sorted(existing - seen)
        removed = await queries.delete_plex_rating_keys(stale) if stale else 0
        logger.info("plex_sync: upserted=%d removed=%d", upserted, removed)
    except Exception:
        logger.exception("plex_sync errored")
