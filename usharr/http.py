"""Process-wide httpx client, so connections and TLS sessions are reused."""

import httpx

shared_client: httpx.AsyncClient | None = None


def client() -> httpx.AsyncClient:
    global shared_client  # noqa: PLW0603
    if shared_client is None:
        shared_client = httpx.AsyncClient()
    return shared_client


async def close() -> None:
    global shared_client  # noqa: PLW0603
    if shared_client is not None:
        await shared_client.aclose()
        shared_client = None
