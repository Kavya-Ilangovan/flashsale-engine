from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from redis.asyncio import Redis

from .config import Settings

_SCRIPTS = Path(__file__).parent / "scripts"


class Store:
    def __init__(self, settings: Settings, redis: Redis | None = None) -> None:
        self.settings = settings
        self.keys = settings.keys
        self.redis: Redis = redis or Redis.from_url(settings.redis_url, decode_responses=True)
        self._reserve = self.redis.register_script((_SCRIPTS / "reserve.lua").read_text())
        self._release = self.redis.register_script((_SCRIPTS / "release.lua").read_text())

    async def close(self) -> None:
        await self.redis.aclose()

    async def reserve(
        self,
        *,
        user_id: str,
        idem_key: str,
        reservation_id: str,
        now_ms: int,
        expires_ms: int,
        payload: dict[str, Any],
    ) -> tuple[int, str]:
        k = self.keys
        code, rid = await self._reserve(
            keys=[k.stock, k.users, k.idem, k.outbox, k.pending, k.reservation(reservation_id)],
            args=[user_id, idem_key, reservation_id, now_ms, expires_ms, json.dumps(payload)],
        )
        return int(code), rid

    async def release_hold(self, reservation_id: str, status: str) -> bool:
        k = self.keys
        released = await self._release(
            keys=[k.stock, k.users, k.pending, k.reservation(reservation_id)],
            args=[reservation_id, status],
        )
        return bool(released)

    async def set_status(self, reservation_id: str, user_id: str, status: str) -> None:
        pipe = self.redis.pipeline(transaction=True)
        pipe.hset(self.keys.reservation(reservation_id), "status", status)
        pipe.publish(
            self.settings.channel,
            json.dumps({"ticket": reservation_id, "userId": user_id, "status": status}),
        )
        await pipe.execute()

    async def get_reservation(self, reservation_id: str) -> dict[str, str] | None:
        data = await self.redis.hgetall(self.keys.reservation(reservation_id))
        return data if data.get("user") else None

    async def ticket_for_user(self, user_id: str) -> str | None:
        return await self.redis.hget(self.keys.users, user_id)

    async def sale_state(self) -> dict[str, Any]:
        start, paused, stock = await self.redis.mget(self.keys.start, self.keys.paused, self.keys.stock)
        return {
            "starts_at": int(start) if start else None,
            "paused": paused == "1",
            "available": max(0, int(stock)) if stock is not None else None,
        }
