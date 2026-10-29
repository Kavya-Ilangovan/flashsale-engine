from __future__ import annotations

import asyncio
import json
import logging
import signal

import aio_pika
from redis.asyncio import Redis

from .config import settings
from .messaging import declare_topology

log = logging.getLogger("flashsale.relay")
BATCH = 100
IDLE_SLEEP_SEC = 0.02


async def run(stop: asyncio.Event) -> None:
    redis = Redis.from_url(settings.redis_url, decode_responses=True)
    connection = await aio_pika.connect_robust(settings.amqp_url)
    channel = await connection.channel(publisher_confirms=True)
    exchange, _ = await declare_topology(channel, settings)
    outbox = settings.keys.outbox
    log.info("relay started")

    while not stop.is_set():
        try:
            batch = await redis.lrange(outbox, 0, BATCH - 1)
            if not batch:
                await asyncio.sleep(IDLE_SLEEP_SEC)
                continue
            await asyncio.gather(
                *(
                    exchange.publish(
                        aio_pika.Message(
                            body=item.encode(),
                            delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
                            content_type="application/json",
                            message_id=json.loads(item)["reservationId"],
                        ),
                        routing_key=settings.queue_main,
                    )
                    for item in batch
                )
            )
            pipe = redis.pipeline(transaction=False)
            for item in batch:
                pipe.lrem(outbox, 1, item)
            await pipe.execute()
        except Exception:
            log.exception("relay iteration failed; retrying")
            await asyncio.sleep(0.5)

    await connection.close()
    await redis.aclose()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    stop = asyncio.Event()

    async def runner() -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, stop.set)
            except NotImplementedError:
                signal.signal(sig, lambda *_: loop.call_soon_threadsafe(stop.set))
        await run(stop)

    asyncio.run(runner())


if __name__ == "__main__":
    main()
