"""Plex library polling + webhook handling.

Polls `/library/sections` once per interval, walks each movie/show
section, and upserts `plex_item` rows. Webhook endpoint (wired in
``app.py``) re-fetches one item by ratingKey and upserts.
"""

import logging
from typing import Any

import httpx
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    model_validator,
)

from usharr import db, plex
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


class LibContainer(Model):
    metadata: list[LibMetadata] = Field(default_factory=list, alias="Metadata")


class LibResponse(Model):
    container: LibContainer = Field(alias="MediaContainer")


async def get_json[T: Model](model: type[T], url: str) -> T:
    token, _, _ = plex.load_auth()
    client_id = plex.get_or_create_client_id()
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


def pick_file(item: LibMetadata) -> str | None:
    return next((p.file for m in item.media for p in m.parts if p.file), None)


def upsert(item: LibMetadata, path_map: dict[str, str]) -> str | None:
    logger.debug("upsert %r", item)
    return db.upsert_plex_item(
        rating_key=item.rating_key,
        item_type=item.type or "movie",
        title=item.title or None,
        year=item.year,
        show_title=item.grandparent_title,
        season_number=item.parent_index,
        episode_number=item.index,
        remote_path=pick_file(item),
        path_map=path_map,
    )


async def sync() -> None:
    """Walk sections and upsert every movie + episode into plex_item."""
    try:
        path_map = get_config().plex.path_map
        try:
            _, server_url, _ = plex.load_auth()
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
                upsert(item, path_map)
                upserted += 1

        existing = db.list_plex_rating_keys()
        stale = sorted(existing - seen)
        removed = db.delete_plex_rating_keys(stale) if stale else 0
        logger.info("plex_sync: upserted=%d removed=%d", upserted, removed)
    except Exception:
        logger.exception("plex_sync errored")


# --- webhook --------------------------------------------------------------


class WebhookMetadata(Model):
    rating_key: str = Field(alias="ratingKey")


class WebhookPayload(Model):
    event: str
    metadata: WebhookMetadata = Field(alias="Metadata")

    @model_validator(mode="before")
    @classmethod
    def parse_json_string(cls, value: Any) -> Any:
        if isinstance(value, str):
            return cls.model_validate_json(value)
        return value


class WebhookForm(Model):
    payload: WebhookPayload


async def library_new(rating_key: str, path_map: dict[str, str]) -> str | None:
    """Handle a Plex `library.new` webhook. Return local path if found."""

    logger.debug("library_new: %s", rating_key)

    _, server_url, _ = plex.load_auth()

    url = f"{server_url}/library/metadata/{rating_key}"
    resp = await get_json(LibResponse, url)
    if not resp.container.metadata:
        return None

    item = resp.container.metadata[0]
    if remote_path := pick_file(item):
        upsert(item, path_map)
        return db.map_remote_path(remote_path, path_map)
    return None
