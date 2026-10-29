from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator

import asyncpg


async def create_pool(dsn: str, max_size: int = 10) -> asyncpg.Pool:
    return await asyncpg.create_pool(dsn, min_size=1, max_size=max_size, command_timeout=5)


@asynccontextmanager
async def transaction(pool: asyncpg.Pool) -> AsyncIterator[asyncpg.Connection]:
    async with pool.acquire() as conn:
        async with conn.transaction():
            yield conn
