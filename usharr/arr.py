"""Shared *arr (Radarr/Sonarr) HTTP fetch."""

import httpx
from pydantic import TypeAdapter, ValidationError


async def get_arr[T](
    adapter: TypeAdapter[list[T]],
    base: str,
    api_key: str,
    resource: str,
) -> list[T]:
    url = f"{base.rstrip('/')}/api/v3/{resource}"
    async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
        resp = await client.get(url, headers={"X-Api-Key": api_key})
    if resp.status_code != 200:
        msg = f"GET {url} → {resp.status_code}"
        raise RuntimeError(msg)
    try:
        return adapter.validate_json(resp.content)
    except ValidationError as exc:
        msg = f"bad {resource} response: {exc}"
        raise RuntimeError(msg) from exc
