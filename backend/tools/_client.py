from __future__ import annotations

import asyncio
import os
import time
import uuid

import httpx

BASE = os.environ.get("BASE", "http://127.0.0.1:3000")
CONCURRENCY = int(os.environ.get("CONCURRENCY", "200"))
_gate: asyncio.Semaphore | None = None


def _limit() -> asyncio.Semaphore:
    global _gate
    if _gate is None:
        _gate = asyncio.Semaphore(CONCURRENCY)
    return _gate


def new_client(max_connections: int = 256) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=BASE,
        timeout=30,
        limits=httpx.Limits(max_connections=max_connections, max_keepalive_connections=max_connections),
    )


async def _post(client: httpx.AsyncClient, url: str, attempts: int = 4, **kwargs) -> httpx.Response:
    for attempt in range(attempts):
        try:
            async with _limit():
                return await client.post(url, **kwargs)
        except httpx.TransportError:
            if attempt == attempts - 1:
                raise
            await asyncio.sleep(0.05 * (attempt + 1))
    raise RuntimeError("unreachable")


async def open_session(client: httpx.AsyncClient) -> dict:
    response = await _post(client, "/api/session", json={})
    response.raise_for_status()
    return response.json()


async def checkout(client: httpx.AsyncClient, token: str, key: str | None = None) -> httpx.Response:
    return await _post(
        client,
        "/api/checkout",
        headers={"Authorization": f"Bearer {token}", "Idempotency-Key": key or str(uuid.uuid4())},
    )


async def wait_for_sale_start(client: httpx.AsyncClient) -> dict:
    sale = (await client.get("/api/sale")).json()
    delay = sale["startsAt"] / 1000 - time.time() + 0.2
    if delay > 0:
        print(f"waiting {delay:.1f}s for the sale to start")
        await asyncio.sleep(delay)
    return sale


class Report:
    def __init__(self) -> None:
        self.ok = True

    def check(self, name: str, condition: bool, detail: str = "") -> None:
        print(f"{'PASS' if condition else 'FAIL'}  {name} {detail}".rstrip())
        self.ok = self.ok and condition
