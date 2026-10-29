from __future__ import annotations

import aio_pika
from aio_pika.abc import AbstractChannel, AbstractExchange, AbstractQueue

from .config import Settings


async def declare_topology(channel: AbstractChannel, settings: Settings) -> tuple[AbstractExchange, AbstractQueue]:
    exchange = await channel.declare_exchange(settings.exchange, aio_pika.ExchangeType.DIRECT, durable=True)
    await channel.declare_queue(settings.queue_dlq, durable=True)
    main = await channel.declare_queue(settings.queue_main, durable=True)
    await channel.declare_queue(
        settings.queue_retry,
        durable=True,
        arguments={"x-dead-letter-exchange": "", "x-dead-letter-routing-key": settings.queue_main},
    )
    await main.bind(exchange, routing_key=settings.queue_main)
    return exchange, main
