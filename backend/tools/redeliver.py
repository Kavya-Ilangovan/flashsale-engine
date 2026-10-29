from __future__ import annotations

import asyncio
import json
import time

import aio_pika
import asyncpg

from flashsale.config import settings


async def main() -> None:
    conn = await asyncpg.connect(settings.database_url)
    orders = await conn.fetch("SELECT order_id, product_id, user_id, idempotency_key FROM orders")
    await conn.close()
    connection = await aio_pika.connect_robust(settings.amqp_url)
    async with connection:
        channel = await connection.channel(publisher_confirms=True)
        for order in orders:
            event = {
                "reservationId": str(order["order_id"]),
                "productId": str(order["product_id"]),
                "userId": order["user_id"],
                "idempotencyKey": order["idempotency_key"],
                "expiresAt": int(time.time() * 1000) + 60_000,
            }
            await channel.default_exchange.publish(
                aio_pika.Message(json.dumps(event).encode(), delivery_mode=aio_pika.DeliveryMode.PERSISTENT),
                routing_key=settings.queue_main,
            )
    print(f"redelivered {len(orders)} events")


if __name__ == "__main__":
    asyncio.run(main())
