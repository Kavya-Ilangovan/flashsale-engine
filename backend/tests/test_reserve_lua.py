import asyncio
import uuid

import pytest_asyncio

from flashsale.store import Store

async def reserve(store: Store, user: str, key: str | None = None):
    rid = str(uuid.uuid4())
    code, ticket = await store.reserve(
        user_id=user, idem_key=f"{user}:{key or uuid.uuid4()}", reservation_id=rid,
        now_ms=1_000, expires_ms=9_999_999_999_999, payload={"reservationId": rid, "userId": user},
    )
    return code, ticket


@pytest_asyncio.fixture
async def store(isolated_settings, redis_client):
    s = Store(isolated_settings, redis=redis_client)
    await redis_client.set(s.keys.stock, 5)
    yield s
    keys = [k async for k in redis_client.scan_iter(match=f"{{p{isolated_settings.product_id}}}:*")]
    if keys:
        await redis_client.delete(*keys)


async def test_exactly_stock_many_winners_under_concurrency(store):
    results = await asyncio.gather(*(reserve(store, f"user{i}") for i in range(200)))
    codes = [code for code, _ in results]
    assert codes.count(1) == 5
    assert codes.count(0) == 195
    assert int(await store.redis.get(store.keys.stock)) == 0
    assert await store.redis.llen(store.keys.outbox) == 5
    assert await store.redis.zcard(store.keys.pending) == 5


async def test_replay_returns_same_reservation_without_new_outbox_entry(store):
    code, ticket = await reserve(store, "alice", key="k1")
    assert code == 1
    replay_code, replay_ticket = await store.reserve(
        user_id="alice", idem_key="alice:k1", reservation_id=str(uuid.uuid4()),
        now_ms=2_000, expires_ms=9_999_999_999_999, payload={},
    )
    assert (replay_code, replay_ticket) == (2, ticket)
    assert await store.redis.llen(store.keys.outbox) == 1


async def test_same_user_with_new_key_gets_no_second_unit(store):
    _, ticket = await reserve(store, "alice")
    code, held = await reserve(store, "alice")
    assert code == 3 and held == ticket
    assert int(await store.redis.get(store.keys.stock)) == 4


async def test_release_is_idempotent_and_restores_exactly_one_unit(store):
    _, ticket = await reserve(store, "alice")
    assert await store.release_hold(ticket, "EXPIRED") is True
    assert await store.release_hold(ticket, "EXPIRED") is False
    assert await store.release_hold(ticket, "EXPIRED") is False
    assert int(await store.redis.get(store.keys.stock)) == 5
    assert await store.redis.hget(store.keys.reservation(ticket), "status") == "EXPIRED"


async def test_released_unit_can_be_reserved_once(store):
    await asyncio.gather(*(reserve(store, f"user{i}") for i in range(5)))
    ticket = await store.ticket_for_user("user0")
    await store.release_hold(ticket, "FAILED")
    results = await asyncio.gather(*(reserve(store, f"late{i}") for i in range(50)))
    assert [c for c, _ in results].count(1) == 1


async def test_missing_stock_key_fails_closed(store):
    await store.redis.delete(store.keys.stock)
    code, _ = await reserve(store, "ghost")
    assert code == 0
