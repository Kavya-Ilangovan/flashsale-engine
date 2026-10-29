from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


def _int(name: str, default: int) -> int:
    return int(os.environ.get(name, default))


@dataclass(frozen=True)
class Keys:

    stock: str
    users: str
    idem: str
    outbox: str
    pending: str
    start: str
    paused: str
    reservation_prefix: str

    @classmethod
    def for_product(cls, product_id: str) -> "Keys":
        tag = f"{{p{product_id}}}"
        return cls(
            stock=f"{tag}:stock",
            users=f"{tag}:users",
            idem=f"{tag}:idem",
            outbox=f"{tag}:outbox",
            pending=f"{tag}:pending",
            start=f"{tag}:start",
            paused=f"{tag}:paused",
            reservation_prefix=f"{tag}:res:",
        )

    def reservation(self, reservation_id: str) -> str:
        return f"{self.reservation_prefix}{reservation_id}"


@dataclass(frozen=True)
class Settings:
    redis_url: str
    database_url: str
    amqp_url: str
    session_secret: str
    session_ttl_sec: int
    cookie_secure: bool

    product_id: str
    product_name: str
    product_price_inr: int
    total_stock: int
    sale_start_delay_sec: int
    sale_start_at_ms: int | None

    reservation_ttl_ms: int
    launch_jitter_ms: int

    rate_user_per_sec: int
    rate_ip_per_sec: int
    admission_per_sec: int
    max_sse_connections: int

    worker_concurrency: int
    max_retries: int
    sweep_interval_sec: float
    reconcile_interval_sec: float
    pending_grace_ms: int
    metrics_port: int

    exchange: str = "checkout"
    queue_main: str = "checkout.reserve"
    queue_retry: str = "checkout.retry"
    queue_dlq: str = "checkout.dlq"
    channel: str = "order-events"

    @property
    def keys(self) -> Keys:
        return Keys.for_product(self.product_id)

    @classmethod
    def from_env(cls) -> "Settings":
        start_at = os.environ.get("SALE_START_AT_MS")
        return cls(
            redis_url=os.environ.get("REDIS_URL", "redis://127.0.0.1:6379/0"),
            database_url=os.environ.get("DATABASE_URL", "postgresql://flash:flash@127.0.0.1:5432/flash"),
            amqp_url=os.environ.get("AMQP_URL", "amqp://guest:guest@127.0.0.1:5672/"),
            session_secret=os.environ.get("SESSION_SECRET", "dev-only-change-me"),
            session_ttl_sec=_int("SESSION_TTL_SEC", 6 * 3600),
            cookie_secure=os.environ.get("COOKIE_SECURE") == "1",
            product_id=os.environ.get("PRODUCT_ID", "42"),
            product_name=os.environ.get("PRODUCT_NAME", "Aurora X1 5G (256 GB)"),
            product_price_inr=_int("PRODUCT_PRICE_INR", 29999),
            total_stock=_int("TOTAL_STOCK", 500),
            sale_start_delay_sec=_int("SALE_START_DELAY_SEC", 45),
            sale_start_at_ms=int(start_at) if start_at else None,
            reservation_ttl_ms=_int("RESERVATION_TTL_MS", 600_000),
            launch_jitter_ms=_int("LAUNCH_JITTER_MS", 0),
            rate_user_per_sec=_int("RATE_USER_PER_SEC", 5),
            rate_ip_per_sec=_int("RATE_IP_PER_SEC", 50),
            admission_per_sec=_int("ADMISSION_PER_SEC_PER_REPLICA", 0),
            max_sse_connections=_int("MAX_SSE_CONNECTIONS", 20_000),
            worker_concurrency=_int("WORKER_CONCURRENCY", 8),
            max_retries=_int("MAX_RETRIES", 5),
            sweep_interval_sec=_int("SWEEP_INTERVAL_MS", 1000) / 1000,
            reconcile_interval_sec=_int("RECONCILE_INTERVAL_MS", 10_000) / 1000,
            pending_grace_ms=_int("PENDING_GRACE_MS", 30_000),
            metrics_port=_int("METRICS_PORT", 9101),
        )


settings = Settings.from_env()
