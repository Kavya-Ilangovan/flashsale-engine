import os
import uuid

import pytest
import pytest_asyncio
from redis.asyncio import Redis

from flashsale.config import Settings


@pytest.fixture
def settings() -> Settings:
    os.environ.setdefault("SESSION_SECRET", "test-secret")
    return Settings.from_env()


@pytest_asyncio.fixture
async def redis_client(settings):
    client = Redis.from_url(settings.redis_url, decode_responses=True)
    try:
        await client.ping()
    except Exception:
        await client.aclose()
        pytest.skip("Redis is not available")
    yield client
    await client.aclose()


@pytest.fixture
def isolated_settings(settings, monkeypatch):
    monkeypatch.setenv("PRODUCT_ID", f"t{uuid.uuid4().hex[:8]}")
    return Settings.from_env()
