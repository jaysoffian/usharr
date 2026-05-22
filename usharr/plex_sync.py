"""Plex library polling + webhook handling.

Polls `/library/sections` once per interval, walks each movie/show
section, and upserts `plex_item` rows. Webhook endpoint (wired in
``app.py``) re-fetches one item by ratingKey and upserts.
"""

import asyncio
import json
import logging
import time
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from usharr import db, plex
from usharr.config import INTERVAL_SECONDS, Config

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
        updated_at=int(time.time()),
    )


async def sync_once(config: Config) -> dict:
    """Walk sections and upsert every movie + episode into plex_item."""
    try:
        _, server_url, _ = plex.load_auth()
    except plex.PlexNotLinkedError:
        logger.info("plex_sync: not linked; skipping")
        return {"skipped": True}

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
            upsert(item, config.plex.path_map)
            upserted += 1

    existing = db.list_plex_rating_keys()
    stale = sorted(existing - seen)
    removed = db.delete_plex_rating_keys(stale) if stale else 0
    logger.info("plex_sync: upserted=%d removed=%d", upserted, removed)
    return {"upserted": upserted, "removed": removed}


async def plex_sync_loop(config: Config) -> None:
    while True:
        try:
            await sync_once(config)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("plex_sync errored")
        try:
            await asyncio.sleep(INTERVAL_SECONDS)
        except asyncio.CancelledError:
            raise


# --- webhook --------------------------------------------------------------


def parse_webhook_payload(raw: str | bytes) -> dict[str, Any]:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        msg = f"invalid webhook JSON: {exc}"
        raise plex.PlexError(msg) from exc
    if not isinstance(data, dict):
        msg = "webhook payload is not a JSON object"
        raise plex.PlexError(msg)
    return data


async def handle_webhook(payload: dict[str, Any], config: Config) -> dict:
    """Handle a Plex `library.new` webhook.

    The payload's Metadata doesn't include the file path, so we re-fetch
    by ratingKey, then map the remote path to local via `path_map`. The
    caller enqueues a probe for that local path — the same code path the
    full scan uses for a freshly-discovered file.
    """
    event = str(payload.get("event") or "")
    if event != "library.new":
        return {"event": event, "action": "ignored"}
    metadata = payload.get("Metadata") or {}
    rating_key = str(metadata.get("ratingKey") or "")
    if not rating_key:
        return {"event": event, "action": "ignored", "reason": "no ratingKey"}

    try:
        _, server_url, _ = plex.load_auth()
    except plex.PlexNotLinkedError:
        return {"event": event, "action": "not_linked"}

    url = f"{server_url.rstrip('/')}/library/metadata/{rating_key}"
    try:
        resp = await get_json(LibResponse, url)
    except plex.PlexError as exc:
        return {"event": event, "action": "fetch_failed", "error": str(exc)}

    items = resp.container.metadata
    if not items:
        return {"event": event, "action": "not_found"}
    item = items[0]
    if not item.rating_key:
        item = LibMetadata(**item.model_dump(by_alias=True) | {"ratingKey": rating_key})

    remote_path = pick_file(item)
    if not remote_path:
        return {"event": event, "action": "no_path", "rating_key": rating_key}

    upsert(item, config.plex.path_map)
    local_path = db.map_remote_path(remote_path, config.plex.path_map)
    return {
        "event": event,
        "action": "enqueued",
        "rating_key": item.rating_key or rating_key,
        "local_path": local_path,
    }
