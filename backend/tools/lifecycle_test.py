from __future__ import annotations

import asyncio
import sys
import time

from tools._client import Report, checkout, new_client, open_session, wait_for_sale_start


async def main() -> int:
    report = Report()
    async with new_client() as client:
        await wait_for_sale_start(client)

        async def status_of(token: str, ticket: str) -> str:
            response = await client.get(f"/api/orders/{ticket}", headers={"Authorization": f"Bearer {token}"})
            return response.json().get("status", "")

        async def until(token: str, ticket: str, wanted: str, timeout: float = 30) -> str:
            deadline = time.monotonic() + timeout
            status = ""
            while time.monotonic() < deadline:
                status = await status_of(token, ticket)
                if status == wanted:
                    break
                await asyncio.sleep(0.15)
            return status

        async def pay(token: str, ticket: str, outcome: str):
            return await client.post(
                f"/api/orders/{ticket}/pay", json={"outcome": outcome}, headers={"Authorization": f"Bearer {token}"}
            )

        a, b, c, d = [(await open_session(client))["token"] for _ in range(4)]
        first = await asyncio.gather(*(checkout(client, t) for t in (a, b, c)))
        report.check("three users reserve the three units", all(r.status_code == 202 for r in first))
        ta, tb, tc = (r.json()["ticket"] for r in first)
        report.check("fourth user is told sold out", (await checkout(client, d)).json().get("code") == "SOLD_OUT")
        for token, ticket in ((a, ta), (b, tb), (c, tc)):
            await until(token, ticket, "PAYMENT_PENDING")

        report.check("payment success confirms", (await pay(a, ta, "success")).json()["status"] == "CONFIRMED")
        report.check("repeated payment is idempotent", (await pay(a, ta, "success")).json()["status"] == "CONFIRMED")
        report.check("payment failure releases", (await pay(b, tb, "fail")).json()["status"] == "FAILED")
        report.check("failed order cannot be confirmed", (await pay(b, tb, "success")).status_code == 409)
        report.check("another user cannot read the order",
                     (await client.get(f"/api/orders/{ta}", headers={"Authorization": f"Bearer {d}"})).status_code == 404)

        report.check("released unit is re-sold", (await checkout(client, d)).status_code == 202)
        extra = (await open_session(client))["token"]
        report.check("no additional unit appears", (await checkout(client, extra)).json().get("code") == "SOLD_OUT")

        print("waiting for the unpaid reservation to expire")
        report.check("unpaid reservation expires", await until(c, tc, "EXPIRED", 45) == "EXPIRED")
        late = await pay(c, tc, "success")
        report.check("late payment cannot confirm a released unit", late.status_code == 409, str(late.json()))
        report.check("expired unit becomes available again", (await checkout(client, extra)).status_code == 202)
    return 0 if report.ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
