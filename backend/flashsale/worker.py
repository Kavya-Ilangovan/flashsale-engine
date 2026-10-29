from __future__ import annotations

import asyncio
import json
import logging
import signal
import time
from datetime import datetime, timezone

import aio_pika
import asyncpg
from aio_pika.abc import AbstractIncomingMessage
from prometheus_client import Counter, Gauge, start_http_server

from .config import settings
from .db import create_pool, transaction
from .lifecycle import log_event, release_order, sync_release
from .messaging import declare_topology
from .store import Store

log = logging.getLogger("flashsale.worker")

PROCESSED = Counter("worker_events_total", "Processed reservation events", ["outcome"])
DRIFT = Gauge("reconcile_drift", "1 when the Redis counter disagrees with PostgreSQL")
INVARIANT = Gauge("db_invariant_violation", "1 when available_stock != total_stock - active orders")


class SoldOutInDatabase(Exception):
    pass


async def persist(pool: asyncpg.Pool, event: dict) -> str:
    if event["expiresAt"] <= int(time.time() * 1000):
        return "EXPIRED_BEFORE_PERSIST"
    expires_at = datetime.fromtimestamp(event["expiresAt"] / 1000, tz=timezone.utc)
    try:
        async with transaction(pool) as conn:
            inserted = await conn.fetchrow(
                """
                INSERT INTO orders (order_id, product_id, user_id, idempotency_key, status, expires_at)
                VALUES ($1::uuid, $2, $3, $4, 'PAYMENT_PENDING', $5)
                ON CONFLICT DO NOTHING RETURNING order_id
                """,
                event["reservationId"],
                int(event["productId"]),
                event["userId"],
                event["idempotencyKey"],
                expires_at,
            )
            if inserted is None:
                existing = await conn.fetchval(
                    "SELECT 1 FROM orders WHERE order_id = $1::uuid", event["reservationId"]
                )
                return "DUPLICATE" if existing else "CONFLICT"
            updated = await conn.execute(
                "UPDATE inventory SET available_stock = available_stock - 1 "
                "WHERE product_id = $1 AND available_stock > 0",
                int(event["productId"]),
            )
            if updated != "UPDATE 1":
                raise SoldOutInDatabase
            await log_event(conn, event["reservationId"], None, "PAYMENT_PENDING", {"source": "worker"})
            return "PERSISTED"
    except SoldOutInDatabase:
        return "SOLD_OUT"


async def reject(store: Store, pool: asyncpg.Pool, event: dict, status: str, reason: str) -> None:
    await pool.execute(
        "INSERT INTO order_events (order_id, from_status, to_status, detail) VALUES ($1::uuid, NULL, $2, $3::jsonb)",
        event["reservationId"],
        status,
        json.dumps({"reason": reason}),
    )
    await store.release_hold(event["reservationId"], status)
    await store.set_status(event["reservationId"], event["userId"], status)


async def handle(store: Store, pool: asyncpg.Pool, event: dict) -> None:
    outcome = await persist(pool, event)
    PROCESSED.labels(outcome).inc()
    rid, uid = event["reservationId"], event["userId"]
    if outcome == "PERSISTED":
        await store.redis.zrem(store.keys.pending, rid)
        await store.set_status(rid, uid, "PAYMENT_PENDING")
    elif outcome == "DUPLICATE":
        status = await pool.fetchval("SELECT status FROM orders WHERE order_id = $1::uuid", rid)
        await store.redis.zrem(store.keys.pending, rid)
        if status:
            await store.set_status(rid, uid, status)
    elif outcome == "SOLD_OUT":
        await reject(store, pool, event, "REJECTED", "SOLD_OUT_IN_DB")
    elif outcome == "CONFLICT":
        await reject(store, pool, event, "REJECTED", "USER_ALREADY_HOLDS_UNIT")
    elif outcome == "EXPIRED_BEFORE_PERSIST":
        await reject(store, pool, event, "EXPIRED", "EXPIRED_BEFORE_PERSIST")
    else:
        raise RuntimeError(f"unknown outcome {outcome}")


class Consumer:
    def __init__(self, store: Store, pool: asyncpg.Pool, channel: aio_pika.abc.AbstractChannel) -> None:
        self.store, self.pool, self.channel = store, pool, channel

    async def on_message(self, message: AbstractIncomingMessage) -> None:
        try:
            event = json.loads(message.body)
        except json.JSONDecodeError:
            await self._dead_letter(message)
            return
        try:
            await handle(self.store, self.pool, event)
            await message.ack()
        except Exception as exc:
            retries = int((message.headers or {}).get("x-retries", 0))
            log.error("reservation %s failed (attempt %d): %s", event.get("reservationId"), retries, exc)
            if retries < settings.max_retries:
                await self._retry(message, retries)
            else:
                PROCESSED.labels("DLQ").inc()
                await self._dead_letter(message)

    async def _retry(self, message: AbstractIncomingMessage, retries: int) -> None:
        delay = min(30.0, 0.5 * 2**retries)
        await self.channel.default_exchange.publish(
            aio_pika.Message(
                body=message.body,
                delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
                message_id=message.message_id,
                expiration=delay,
                headers={"x-retries": retries + 1},
            ),
            routing_key=settings.queue_retry,
        )
        await message.ack()

    async def _dead_letter(self, message: AbstractIncomingMessage) -> None:
        await self.channel.default_exchange.publish(
            aio_pika.Message(
                body=message.body, delivery_mode=aio_pika.DeliveryMode.PERSISTENT, message_id=message.message_id
            ),
            routing_key=settings.queue_dlq,
        )
        await message.ack()


async def sweep(store: Store, pool: asyncpg.Pool) -> None:
    rows = await pool.fetch(
        "SELECT order_id FROM orders WHERE status = 'PAYMENT_PENDING' AND expires_at <= now() "
        "ORDER BY expires_at LIMIT 100"
    )
    for row in rows:
        order_id = str(row["order_id"])
        async with transaction(pool) as conn:
            released = await release_order(conn, order_id, "EXPIRED", expired_only=True)
        if released:
            PROCESSED.labels("EXPIRED").inc()
            await sync_release(store, pool, order_id, released.user_id, "EXPIRED")


class Reconciler:
    def __init__(self, store: Store, pool: asyncpg.Pool, main_queue: aio_pika.abc.AbstractQueue,
                 retry_queue: aio_pika.abc.AbstractQueue) -> None:
        self.store, self.pool = store, pool
        self.main_queue, self.retry_queue = main_queue, retry_queue
        self._previous_drift = False

    async def run_once(self) -> None:
        keys = self.store.keys
        redis = self.store.redis

        for row in await self.pool.fetch(
            "SELECT order_id, user_id, status FROM orders WHERE redis_synced = FALSE LIMIT 100"
        ):
            await sync_release(self.store, self.pool, str(row["order_id"]), row["user_id"], row["status"])

        cutoff = int(time.time() * 1000) - settings.pending_grace_ms
        for rid in await redis.zrangebyscore(keys.pending, "-inf", cutoff):
            row = await self.pool.fetchrow("SELECT user_id, status FROM orders WHERE order_id = $1::uuid", rid)
            if row:
                await redis.zrem(keys.pending, rid)
                await self.store.set_status(rid, row["user_id"], row["status"])
                continue
            user = await redis.hget(keys.reservation(rid), "user")
            await self.store.release_hold(rid, "EXPIRED")
            if user:
                await self.store.set_status(rid, user, "EXPIRED")
            log.warning("released reservation %s that never reached PostgreSQL", rid)

        inv = await self.pool.fetchrow(
            """
            SELECT i.total_stock, i.available_stock,
                   (SELECT count(*) FROM orders o
                     WHERE o.product_id = i.product_id AND o.status IN ('PAYMENT_PENDING', 'CONFIRMED'))::int AS active
              FROM inventory i WHERE i.product_id = $1
            """,
            int(settings.product_id),
        )
        if inv is None:
            return
        violated = inv["available_stock"] != inv["total_stock"] - inv["active"]
        INVARIANT.set(1 if violated else 0)
        if violated:
            log.error("database invariant violated: %s", dict(inv))

        if not await redis.exists(keys.stock):
            await self._rebuild_if_drained(inv["available_stock"])
            return

        stock = int(await redis.get(keys.stock))
        pending = await redis.zcard(keys.pending)
        unsynced = await self.pool.fetchval("SELECT count(*) FROM orders WHERE redis_synced = FALSE")
        expected = inv["available_stock"] - pending + unsynced
        drift = stock != expected
        DRIFT.set(1 if drift and self._previous_drift else 0)
        if drift and self._previous_drift:
            log.error("redis counter drift: redis=%d expected=%d", stock, expected)
        self._previous_drift = drift

    async def _rebuild_if_drained(self, available: int) -> None:
        redis, keys = self.store.redis, self.store.keys
        refreshed_main = await self.main_queue.channel.get_queue(settings.queue_main, ensure=True)
        refreshed_retry = await self.main_queue.channel.get_queue(settings.queue_retry, ensure=True)
        queued = refreshed_main.declaration_result.message_count + refreshed_retry.declaration_result.message_count
        if queued or await redis.llen(keys.outbox):
            log.warning("redis state missing; failing closed until queues drain")
            return
        pipe = redis.pipeline(transaction=True)
        pipe.set(keys.stock, available, nx=True)
        for row in await self.pool.fetch(
            "SELECT order_id, user_id, idempotency_key FROM orders "
            "WHERE product_id = $1 AND status IN ('PAYMENT_PENDING', 'CONFIRMED')",
            int(settings.product_id),
        ):
            pipe.hset(keys.users, row["user_id"], str(row["order_id"]))
            pipe.hset(keys.idem, row["idempotency_key"], str(row["order_id"]))
        await pipe.execute()
        log.warning("rebuilt Redis state from PostgreSQL")


async def periodic(name: str, interval: float, fn) -> None:
    while True:
        try:
            await fn()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("%s iteration failed", name)
        await asyncio.sleep(interval)


async def run(stop: asyncio.Event) -> None:
    store = Store(settings)
    pool = await create_pool(settings.database_url, max_size=settings.worker_concurrency + 4)
    connection = await aio_pika.connect_robust(settings.amqp_url)
    channel = await connection.channel(publisher_confirms=True)
    await channel.set_qos(prefetch_count=settings.worker_concurrency)
    _, main_queue = await declare_topology(channel, settings)
    retry_queue = await channel.get_queue(settings.queue_retry)

    start_http_server(settings.metrics_port)
    consumer = Consumer(store, pool, channel)
    await main_queue.consume(consumer.on_message)
    reconciler = Reconciler(store, pool, main_queue, retry_queue)
    tasks = [
        asyncio.create_task(periodic("sweep", settings.sweep_interval_sec, lambda: sweep(store, pool))),
        asyncio.create_task(periodic("reconcile", settings.reconcile_interval_sec, reconciler.run_once)),
    ]
    log.info("worker consuming %s (concurrency %d)", settings.queue_main, settings.worker_concurrency)

    await stop.wait()
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    await connection.close()
    await pool.close()
    await store.close()


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
