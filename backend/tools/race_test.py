from __future__ import annotations

import asyncio
import collections
import os
import sys
import time
import uuid

import asyncpg

from flashsale.config import settings
from tools._client import Report, checkout, new_client, open_session, wait_for_sale_start

USERS = int(os.environ.get("USERS", "3000"))


async def main() -> int:
    report = Report()
    async with new_client() as client:
        sale = (await client.get("/api/sale")).json()
        print(f"stock={sale['totalStock']} available={sale['available']} users={USERS}")
        sessions = await asyncio.gather(*(open_session(client) for _ in range(USERS)))
        await wait_for_sale_start(client)

        async def attempt(session: dict) -> list[tuple[str, int, dict]]:
            key = str(uuid.uuid4())
            token = session["token"]
            results = await asyncio.gather(
                checkout(client, token, key), checkout(client, token, key), checkout(client, token)
            )
            return [(session["userId"], r.status_code, r.json()) for r in results]

        started = time.perf_counter()
        batches = await asyncio.gather(*(attempt(s) for s in sessions))
        elapsed = time.perf_counter() - started

    results = [item for batch in batches for item in batch]
    tally = collections.Counter(f"{status} {body.get('code') or body.get('status')}" for _, status, body in results)
    print(f"{len(results)} requests in {elapsed:.2f}s ({len(results) / elapsed:.0f} req/s)")
    for outcome, count in sorted(tally.items()):
        print(f"  {outcome:<28}{count}")

    tickets: dict[str, set[str]] = collections.defaultdict(set)
    for user, status, body in results:
        if status in (200, 202):
            tickets[user].add(body["ticket"])
    reserved = {t for ts in tickets.values() for t in ts}

    report.check("distinct reservations <= stock", len(reserved) <= sale["totalStock"],
                 f"({len(reserved)}/{sale['totalStock']})")
    report.check("stock fully allocated under excess demand",
                 USERS < sale["totalStock"] or len(reserved) == sale["totalStock"])
    report.check("every user holds at most one ticket", all(len(ts) == 1 for ts in tickets.values()))
    report.check("no 5xx or network errors", not any(status >= 500 for _, status, _ in results))

    conn = await asyncpg.connect(settings.database_url)
    try:
        for _ in range(120):
            active = await conn.fetchval(
                "SELECT count(*) FROM orders WHERE status IN ('PAYMENT_PENDING', 'CONFIRMED')"
            )
            if active >= len(reserved):
                break
            await asyncio.sleep(0.5)
        row = await conn.fetchrow(
            """
            SELECT i.total_stock, i.available_stock,
                   (SELECT count(*) FROM orders WHERE status IN ('PAYMENT_PENDING', 'CONFIRMED')) AS active,
                   (SELECT count(*) FROM (SELECT user_id FROM orders
                      WHERE status IN ('PAYMENT_PENDING', 'CONFIRMED') GROUP BY user_id HAVING count(*) > 1) d) AS dup
              FROM inventory i LIMIT 1
            """
        )
    finally:
        await conn.close()
    print("postgres:", dict(row))
    report.check("PostgreSQL active orders == accepted reservations", row["active"] == len(reserved),
                 f"({row['active']} vs {len(reserved)})")
    report.check("PostgreSQL active orders <= stock", row["active"] <= row["total_stock"])
    report.check("available_stock == total_stock - active", row["available_stock"] == row["total_stock"] - row["active"])
    report.check("no user holds two active orders", row["dup"] == 0)
    return 0 if report.ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
