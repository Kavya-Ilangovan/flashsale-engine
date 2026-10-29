from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass, field

import asyncpg
from redis.asyncio import Redis

from flashsale.config import settings

BUYERS = int(os.environ.get("BUYERS", "2000"))
STOCK = int(os.environ.get("STOCK", "500"))
POOL = int(os.environ.get("POOL", "50"))
MAX_OPTIMISTIC_ATTEMPTS = 25

SETUP = """
DROP TABLE IF EXISTS bench_orders; DROP TABLE IF EXISTS bench_inventory;
CREATE TABLE bench_inventory (id INT PRIMARY KEY, stock INT NOT NULL, version INT NOT NULL DEFAULT 0);
CREATE TABLE bench_orders (id BIGSERIAL PRIMARY KEY, buyer INT NOT NULL);
"""

GATE_SCRIPT = """
local stock = tonumber(redis.call('GET', KEYS[1]))
if stock and stock > 0 then redis.call('DECR', KEYS[1]); return 1 end
return 0
"""


@dataclass
class Result:
    name: str
    elapsed: float = 0.0
    sold: int = 0
    stock_left: int = 0
    statements: int = 0
    retries: int = 0
    gave_up: int = 0
    db_buyers: int = 0
    extra: dict = field(default_factory=dict)

    @property
    def oversold(self) -> int:
        return max(0, self.sold - STOCK)


class Counter:
    def __init__(self) -> None:
        self.statements = 0
        self.retries = 0
        self.gave_up = 0
        self.db_buyers = 0


async def naive_select_then_update(pool, buyer: int, c: Counter) -> None:
    async with pool.acquire() as conn:
        c.db_buyers += 1
        stock = await conn.fetchval("SELECT stock FROM bench_inventory WHERE id = 1")
        c.statements += 1
        if stock > 0:
            await conn.execute("UPDATE bench_inventory SET stock = stock - 1 WHERE id = 1")
            await conn.execute("INSERT INTO bench_orders (buyer) VALUES ($1)", buyer)
            c.statements += 2


async def pessimistic_for_update(pool, buyer: int, c: Counter) -> None:
    async with pool.acquire() as conn:
        c.db_buyers += 1
        async with conn.transaction():
            stock = await conn.fetchval("SELECT stock FROM bench_inventory WHERE id = 1 FOR UPDATE")
            c.statements += 1
            if stock > 0:
                await conn.execute("UPDATE bench_inventory SET stock = stock - 1 WHERE id = 1")
                await conn.execute("INSERT INTO bench_orders (buyer) VALUES ($1)", buyer)
                c.statements += 2


async def optimistic_version_check(pool, buyer: int, c: Counter) -> None:
    async with pool.acquire() as conn:
        c.db_buyers += 1
        for attempt in range(MAX_OPTIMISTIC_ATTEMPTS):
            row = await conn.fetchrow("SELECT stock, version FROM bench_inventory WHERE id = 1")
            c.statements += 1
            if row["stock"] <= 0:
                return
            updated = await conn.execute(
                "UPDATE bench_inventory SET stock = stock - 1, version = version + 1 WHERE id = 1 AND version = $1",
                row["version"],
            )
            c.statements += 1
            if updated == "UPDATE 1":
                await conn.execute("INSERT INTO bench_orders (buyer) VALUES ($1)", buyer)
                c.statements += 1
                return
            c.retries += 1
        c.gave_up += 1


async def conditional_atomic_update(pool, buyer: int, c: Counter) -> None:
    async with pool.acquire() as conn:
        c.db_buyers += 1
        async with conn.transaction():
            row = await conn.fetchrow(
                "UPDATE bench_inventory SET stock = stock - 1 WHERE id = 1 AND stock > 0 RETURNING stock"
            )
            c.statements += 1
            if row is not None:
                await conn.execute("INSERT INTO bench_orders (buyer) VALUES ($1)", buyer)
                c.statements += 1


async def redis_gate_then_conditional_update(pool, buyer: int, c: Counter, gate) -> None:
    if not await gate(buyer):
        return
    await conditional_atomic_update(pool, buyer, c)


async def run_strategy(name: str, pool: asyncpg.Pool, redis: Redis, make_task) -> Result:
    await pool.execute(SETUP)
    await pool.execute("INSERT INTO bench_inventory (id, stock) VALUES (1, $1)", STOCK)
    await redis.set("bench:stock", STOCK)
    counter = Counter()
    started = time.perf_counter()
    await asyncio.gather(*(make_task(buyer, counter) for buyer in range(BUYERS)))
    elapsed = time.perf_counter() - started
    sold = await pool.fetchval("SELECT count(*) FROM bench_orders")
    stock_left = await pool.fetchval("SELECT stock FROM bench_inventory WHERE id = 1")
    return Result(name, elapsed, sold, stock_left, counter.statements, counter.retries, counter.gave_up, counter.db_buyers)


async def main() -> None:
    pool = await asyncpg.create_pool(settings.database_url, min_size=POOL, max_size=POOL)
    redis = Redis.from_url(settings.redis_url, decode_responses=True)
    gate_script = redis.register_script(GATE_SCRIPT)

    async def gate(_buyer: int) -> bool:
        return bool(await gate_script(keys=["bench:stock"]))

    strategies = [
        ("1. SELECT then UPDATE (legacy)", lambda b, c: naive_select_then_update(pool, b, c)),
        ("2. Pessimistic (FOR UPDATE)", lambda b, c: pessimistic_for_update(pool, b, c)),
        ("3. Optimistic (version check)", lambda b, c: optimistic_version_check(pool, b, c)),
        ("4. Conditional atomic UPDATE", lambda b, c: conditional_atomic_update(pool, b, c)),
        ("5. Redis atomic gate + conditional UPDATE", lambda b, c: redis_gate_then_conditional_update(pool, b, c, gate)),
    ]
    print(f"buyers={BUYERS} stock={STOCK} db_pool={POOL}\n")
    header = f"{'strategy':<44}{'sold':>6}{'oversold':>10}{'time(s)':>9}{'db buyers':>11}{'statements':>12}{'retries':>9}{'gave up':>9}"
    print(header)
    print("-" * len(header))
    for name, make in strategies:
        r = await run_strategy(name, pool, redis, make)
        print(f"{r.name:<44}{r.sold:>6}{r.oversold:>10}{r.elapsed:>9.2f}{r.db_buyers:>11}{r.statements:>12}{r.retries:>9}{r.gave_up:>9}")

    await pool.execute("DROP TABLE IF EXISTS bench_orders; DROP TABLE IF EXISTS bench_inventory;")
    await redis.delete("bench:stock")
    await pool.close()
    await redis.aclose()


if __name__ == "__main__":
    asyncio.run(main())
