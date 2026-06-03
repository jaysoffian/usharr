"""Plex PIN-based OAuth, server discovery, and rating_key lookup.

Auth flow:
    1. ``usharr auth`` creates a PIN at plex.tv/api/v2/pins and prints a URL.
    2. User opens the URL, logs in at plex.tv, clicks "Allow".
    3. usharr polls the PIN until ``authToken`` is set, then discovers a
       reachable server via /api/v2/resources and stores ``token`` +
       ``server_url`` + ``server_name`` + ``client_id`` in the plex_auth row.
"""

import asyncio
import logging
import time
import uuid
from typing import Any

import httpx
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    ValidationError,
    model_validator,
)

from usharr.models import PlexAuth

logger = logging.getLogger(__name__)

PLEX_TV = "https://plex.tv"
PRODUCT = "usharr"
VERSION = "0.1.0"


class PlexError(RuntimeError):
    pass


class PlexNotLinkedError(PlexError):
    """No Plex credentials are on file; run ``usharr auth``."""


# --- response models ------------------------------------------------------


class PlexModel(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")


class PlexPin(PlexModel):
    """Response from POST/GET /api/v2/pins (subset we use)."""

    id: int
    code: str
    auth_token: str | None = Field(default=None, alias="authToken")


class PlexConnection(PlexModel):
    uri: str
    local: bool = False
    protocol: str = ""


class PlexResource(PlexModel):
    name: str = ""
    provides: str = ""
    owned: bool = False
    connections: list[PlexConnection] = Field(default_factory=list)


class PlexUser(PlexModel):
    email: str = ""


class Identity(PlexModel):
    machine_identifier: str = Field(default="", alias="machineIdentifier")


class PlexIdentityResponse(PlexModel):
    container: Identity = Field(alias="MediaContainer")


class Part(PlexModel):
    file: str = ""


class Media(PlexModel):
    parts: list[Part] = Field(default_factory=list, alias="Part")


class Metadata(PlexModel):
    media: list[Media] = Field(default_factory=list, alias="Media")


class MediaContainer(PlexModel):
    metadata: list[Metadata] = Field(default_factory=list, alias="Metadata")


class PlexMetadataResponse(PlexModel):
    container: MediaContainer = Field(alias="MediaContainer")


class PlexWebhookPayload(PlexModel):
    event: str

    @model_validator(mode="before")
    @classmethod
    def parse_json_string(cls, value: Any) -> Any:
        if isinstance(value, str):
            return cls.model_validate_json(value)
        return value


class PlexWebhookForm(PlexModel):
    payload: PlexWebhookPayload


RESOURCES_ADAPTER: TypeAdapter[list[PlexResource]] = TypeAdapter(list[PlexResource])


def parse[T: BaseModel](model: type[T], payload: bytes, endpoint: str) -> T:
    try:
        return model.model_validate_json(payload)
    except ValidationError as exc:
        msg = f"{endpoint}: bad response shape: {exc}"
        raise PlexError(msg) from exc


# --- auth state (single-row plex_auth) ------------------------------------


async def _auth_row() -> PlexAuth:
    row = await PlexAuth.objects.get_or_none(id=1)
    if row is None:
        row = await PlexAuth.objects.create(id=1)
    return row


async def get_or_create_client_id() -> str:
    row = await _auth_row()
    if row.client_id:
        return row.client_id
    client_id = str(uuid.uuid4())
    await PlexAuth.objects.filter(id=1).update(client_id=client_id)
    return client_id


async def save_auth(token: str, server_url: str, server_name: str) -> None:
    await _auth_row()
    await PlexAuth.objects.filter(id=1).update(
        token=token, server_url=server_url, server_name=server_name
    )


async def clear_auth() -> None:
    await PlexAuth.objects.filter(id=1).update(
        token=None, server_url=None, server_name=None, machine_id=None
    )


async def load_auth() -> tuple[str, str, str]:
    """Return (token, server_url, server_name) or raise PlexNotLinkedError."""
    row = await PlexAuth.objects.get_or_none(id=1)
    if not row or not row.token or not row.server_url:
        msg = "Plex is not linked. Run `usharr auth`."
        raise PlexNotLinkedError(msg)
    return row.token, row.server_url, row.server_name or ""


async def get_server_url() -> str | None:
    """Return the stored Plex server URL, or None if unlinked."""
    row = await PlexAuth.objects.get_or_none(id=1)
    return row.server_url if row else None


def headers(client_id: str, token: str | None = None) -> dict[str, str]:
    h = {
        "Accept": "application/json",
        "X-Plex-Product": PRODUCT,
        "X-Plex-Version": VERSION,
        "X-Plex-Platform": "Docker",
        "X-Plex-Device": "Server",
        "X-Plex-Device-Name": "usharr",
        "X-Plex-Client-Identifier": client_id,
    }
    if token:
        h["X-Plex-Token"] = token
    return h


def auth_url(client_id: str, code: str) -> str:
    return (
        f"https://app.plex.tv/auth#?clientID={client_id}"
        f"&code={code}"
        f"&context%5Bdevice%5D%5Bproduct%5D={PRODUCT}"
    )


# --- OAuth: PIN flow ------------------------------------------------------


async def create_pin(client_id: str) -> PlexPin:
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.post(
            f"{PLEX_TV}/api/v2/pins",
            headers=headers(client_id),
            data={"strong": "true"},
        )
    if r.status_code != 201:
        msg = f"POST /api/v2/pins → {r.status_code}: {r.text[:200]}"
        raise PlexError(msg)
    return parse(PlexPin, r.content, "POST /api/v2/pins")


async def check_pin(client_id: str, pin_id: int) -> str | None:
    """Return authToken if the PIN has been authorized, else None."""
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.get(
            f"{PLEX_TV}/api/v2/pins/{pin_id}",
            headers=headers(client_id),
        )
    if r.status_code == 404:
        msg = f"PIN {pin_id} expired or unknown"
        raise PlexError(msg)
    if r.status_code != 200:
        msg = f"GET /api/v2/pins/{pin_id} → {r.status_code}"
        raise PlexError(msg)
    return parse(PlexPin, r.content, f"GET /api/v2/pins/{pin_id}").auth_token


async def poll_pin(
    client_id: str,
    pin_id: int,
    *,
    timeout: float = 300.0,
    interval: float = 2.0,
) -> str | None:
    """Poll ``check_pin`` until a token appears or the deadline passes."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        token = await check_pin(client_id, pin_id)
        if token:
            return token
        await asyncio.sleep(interval)
    return None


# --- Server discovery -----------------------------------------------------


async def discover_server(client_id: str, token: str) -> tuple[str, str]:
    """Pick a reachable owned server. Returns (url, name)."""
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.get(
            f"{PLEX_TV}/api/v2/resources",
            headers=headers(client_id, token),
            params={"includeHttps": "1", "includeRelay": "0"},
        )
    if r.status_code != 200:
        msg = f"GET /api/v2/resources → {r.status_code}"
        raise PlexError(msg)
    try:
        resources = RESOURCES_ADAPTER.validate_json(r.content)
    except ValidationError as exc:
        msg = f"GET /api/v2/resources: bad response shape: {exc}"
        raise PlexError(msg) from exc

    servers = [s for s in resources if "server" in s.provides.split(",") and s.owned]
    if not servers:
        msg = "no owned Plex servers are registered to this account"
        raise PlexError(msg)

    def rank(c: PlexConnection) -> tuple[int, int]:
        # local first; http before https to avoid a TLS handshake on LAN
        return (0 if c.local else 1, 0 if c.protocol == "http" else 1)

    for s in servers:
        for c in sorted(s.connections, key=rank):
            if await reachable(c.uri, client_id, token):
                return c.uri, s.name or "Plex Server"

    msg = "no reachable connection for any owned Plex server"
    raise PlexError(msg)


async def reachable(url: str, client_id: str, token: str) -> bool:
    try:
        async with httpx.AsyncClient(timeout=3.0, verify=True) as client:
            r = await client.get(f"{url}/identity", headers=headers(client_id, token))
        return r.status_code == 200
    except httpx.HTTPError:
        return False


async def account_email(client_id: str, token: str) -> str:
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.get(
            f"{PLEX_TV}/api/v2/user",
            headers=headers(client_id, token),
        )
    if r.status_code != 200:
        return ""
    return parse(PlexUser, r.content, "GET /api/v2/user").email


# --- machineIdentifier ----------------------------------------------------


async def get_machine_identifier() -> str | None:
    """Cached fetch of the Plex server's machineIdentifier (for deep-links)."""
    row = await PlexAuth.objects.get_or_none(id=1)
    if row and row.machine_id:
        return row.machine_id
    try:
        token, server_url, _ = await load_auth()
    except PlexNotLinkedError:
        return None
    client_id = await get_or_create_client_id()
    endpoint = f"{server_url.rstrip('/')}/identity"
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get(endpoint, headers=headers(client_id, token))
    except httpx.HTTPError as exc:
        logger.warning("GET /identity failed: %s", exc)
        return None
    if r.status_code != 200:
        logger.warning("GET /identity → %s", r.status_code)
        return None
    try:
        resp = parse(PlexIdentityResponse, r.content, "GET /identity")
    except PlexError:
        return None
    if not resp.container.machine_identifier:
        return None
    await PlexAuth.objects.filter(id=1).update(
        machine_id=resp.container.machine_identifier
    )
    return resp.container.machine_identifier


# --- rating_key → file paths ----------------------------------------------


async def resolve_rating_key(rating_key: str) -> list[str]:
    """Return all Plex-side file paths for an item's Parts."""
    token, server_url, _ = await load_auth()
    client_id = await get_or_create_client_id()
    endpoint = f"{server_url.rstrip('/')}/library/metadata/{rating_key}"
    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            r = await client.get(endpoint, headers=headers(client_id, token))
        except httpx.HTTPError as exc:
            msg = f"request failed: {exc}"
            raise PlexError(msg) from exc
    if r.status_code == 401:
        msg = "Plex rejected the stored token. Re-run `usharr auth`."
        raise PlexNotLinkedError(msg)
    if r.status_code != 200:
        msg = f"GET {endpoint} → {r.status_code}"
        raise PlexError(msg)
    resp = parse(PlexMetadataResponse, r.content, f"GET {endpoint}")
    return [
        part.file
        for item in resp.container.metadata
        for media in item.media
        for part in media.parts
        if part.file
    ]
