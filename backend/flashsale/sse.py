from __future__ import annotations

import asyncio
import json
import logging
from collections import defaultdict
from typing import AsyncIterator

from redis.asyncio import Redis

log = logging.getLogger(__name__)
HEARTBEAT_SEC = 15


class SseHub:
    def __init__(self, redis: Redis, channel: str, max_connections: int) -> None:
        self._redis = redis
        self._channel = channel
        self._max = max_connections
        self._subscribers: dict[str, set[asyncio.Queue[str]]] = defaultdict(set)
        self._count = 0
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._listen(), name="sse-listener")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)

    @property
    def has_capacity(self) -> bool:
        return self._count < self._max

    async def _listen(self) -> None:
        while True:
            pubsub = self._redis.pubsub()
            try:
                await pubsub.subscribe(self._channel)
                async for message in pubsub.listen():
                    if message.get("type") != "message":
                        continue
                    try:
                        event = json.loads(message["data"])
                    except json.JSONDecodeError:
                        continue
                    for queue in tuple(self._subscribers.get(event.get("ticket"), ())):
                        queue.put_nowait(event["status"])
            except asyncio.CancelledError:
                await pubsub.aclose()
                raise
            except Exception:
                log.exception("SSE listener failed; reconnecting")
                await asyncio.sleep(1)

    async def stream(self, ticket: str, initial_status: str) -> AsyncIterator[str]:
        queue: asyncio.Queue[str] = asyncio.Queue()
        self._subscribers[ticket].add(queue)
        self._count += 1
        try:
            yield "retry: 3000\n\n"
            yield self._frame(initial_status)
            while True:
                try:
                    status = await asyncio.wait_for(queue.get(), HEARTBEAT_SEC)
                except asyncio.TimeoutError:
                    yield ": heartbeat\n\n"
                    continue
                yield self._frame(status)
        finally:
            self._subscribers[ticket].discard(queue)
            if not self._subscribers[ticket]:
                self._subscribers.pop(ticket, None)
            self._count -= 1

    @staticmethod
    def _frame(status: str) -> str:
        return f"event: status\ndata: {json.dumps({'status': status})}\n\n"
