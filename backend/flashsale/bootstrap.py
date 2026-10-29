from __future__ import annotations

import argparse
import asyncio
import logging
import time
from datetime import datetime, timezone
from pathlib import Path

import aio_pika
import asyncpg
from redis.asyncio import Redis

from .config import settings
from .messaging import declare_topology

log = logging.getLogger("flashsale.bootstrap")


async def purge_queues() -> None:
    try:
        connection = await aio_pika.connect_robust(settings.amqp_url)
    except Exception as exc:
        log.warning("queue purge skipped: %s", exc)
        return
    async with connection:
        channel = await connection.channel()
        await declare_topology(channel, settings)
        for name in (settings.queue_main, settings.queue_retry, settings.queue_dlq):
            queue = await channel.get_queue(name)
            await queue.purge()


async def run(reset: bool) -> None:
    schema = (Path(__file__).parent / "schema.sql").read_text()
    conn = await asyncpg.connect(settings.database_url)
    redis = Redis.from_url(settings.redis_url, decode_responses=True)
    keys = settings.keys
    try:
        await conn.execute(schema)
        async with conn.transaction():
            if reset:
                await conn.execute("TRUNCATE orders, order_events RESTART IDENTITY")
            await conn.execute(
                """
                INSERT INTO inventory (product_id, name, price_inr, total_stock, available_stock)
                VALUES ($1, $2, $3, $4, $4)
                ON CONFLICT (product_id) DO UPDATE
                   SET name = EXCLUDED.name, price_inr = EXCLUDED.price_inr
                """
                + (", total_stock = EXCLUDED.total_stock, available_stock = EXCLUDED.total_stock" if reset else ""),
                int(settings.product_id),
                settings.product_name,
                settings.product_price_inr,
                settings.total_stock,
            )
        if reset:
            async for key in redis.scan_iter(match=f"{{p{settings.product_id}}}:*", count=500):
                await redis.delete(key)
            await purge_queues()

        available = await conn.fetchval(
            "SELECT available_stock FROM inventory WHERE product_id = $1", int(settings.product_id)
        )
        seeded = await redis.set(keys.stock, available, nx=True)
        start_ms = settings.sale_start_at_ms or int((time.time() + settings.sale_start_delay_sec) * 1000)
        await redis.set(keys.start, start_ms, nx=True)
        stored_start = int(await redis.get(keys.start))
        log.info(
            "product=%s stock=%s (%s) starts_at=%s",
            settings.product_id,
            await redis.get(keys.stock),
            "seeded" if seeded else "kept",
            datetime.fromtimestamp(stored_start / 1000, tz=timezone.utc).isoformat(),
        )
    finally:
        await conn.close()
        await redis.aclose()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--reset", action="store_true", help="wipe orders, Redis state and queues")
    asyncio.run(run(parser.parse_args().reset))


if __name__ == "__main__":
    main()
