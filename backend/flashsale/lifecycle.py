from __future__ import annotations

import json
from dataclasses import dataclass

import asyncpg

from .store import Store

RELEASE_TARGETS = frozenset({"FAILED", "EXPIRED", "CANCELLED"})


@dataclass(frozen=True)
class Released:
    user_id: str
    product_id: int


async def log_event(
    conn: asyncpg.Connection, order_id: str, from_status: str | None, to_status: str, detail: dict | None = None
) -> None:
    await conn.execute(
        "INSERT INTO order_events (order_id, from_status, to_status, detail) VALUES ($1::uuid, $2, $3, $4::jsonb)",
        order_id,
        from_status,
        to_status,
        json.dumps(detail) if detail else None,
    )


async def confirm_order(conn: asyncpg.Connection, order_id: str, user_id: str) -> bool:
    row = await conn.fetchrow(
        """
        UPDATE orders SET status = 'CONFIRMED', updated_at = now()
         WHERE order_id = $1::uuid AND user_id = $2 AND status = 'PAYMENT_PENDING' AND expires_at > now()
        RETURNING order_id
        """,
        order_id,
        user_id,
    )
    if row is None:
        return False
    await log_event(conn, order_id, "PAYMENT_PENDING", "CONFIRMED")
    return True


async def release_order(
    conn: asyncpg.Connection,
    order_id: str,
    target: str,
    *,
    user_id: str | None = None,
    expired_only: bool = False,
) -> Released | None:
    if target not in RELEASE_TARGETS:
        raise ValueError(f"invalid release target: {target}")
    row = await conn.fetchrow(
        """
        UPDATE orders SET status = $2, updated_at = now(), redis_synced = FALSE
         WHERE order_id = $1::uuid AND status = 'PAYMENT_PENDING'
           AND ($3::text IS NULL OR user_id = $3)
           AND (NOT $4::boolean OR expires_at <= now())
        RETURNING product_id, user_id
        """,
        order_id,
        target,
        user_id,
        expired_only,
    )
    if row is None:
        return None
    await conn.execute(
        "UPDATE inventory SET available_stock = available_stock + 1 WHERE product_id = $1", row["product_id"]
    )
    await log_event(conn, order_id, "PAYMENT_PENDING", target)
    return Released(user_id=row["user_id"], product_id=row["product_id"])


async def sync_release(store: Store, pool: asyncpg.Pool, order_id: str, user_id: str, status: str) -> None:
    await store.release_hold(order_id, status)
    await pool.execute(
        "UPDATE orders SET redis_synced = TRUE WHERE order_id = $1::uuid AND redis_synced = FALSE", order_id
    )
    await store.set_status(order_id, user_id, status)
