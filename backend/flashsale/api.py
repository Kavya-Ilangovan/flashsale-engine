from __future__ import annotations

import logging
import re
import secrets
import time
import uuid
from contextlib import asynccontextmanager
from typing import Literal

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest
from pydantic import BaseModel

from . import lifecycle
from .auth import issue_token, verify_token
from .config import Settings, settings as default_settings
from .db import create_pool, transaction
from .limiter import FixedWindowLimiter
from .sse import SseHub
from .store import Store

log = logging.getLogger("flashsale.api")

TERMINAL = frozenset({"CONFIRMED", "FAILED", "EXPIRED", "CANCELLED", "REJECTED"})
IDEMPOTENCY_KEY = re.compile(r"^[A-Za-z0-9-]{16,64}$")
USER_ID = re.compile(r"^[A-Za-z0-9_-]{3,40}$")
TICKET = re.compile(r"^[0-9a-f-]{36}$")
SALE_CACHE_SEC = 0.25

CHECKOUT_RESULTS = Counter("checkout_requests_total", "Checkout outcomes", ["result"])
CHECKOUT_LATENCY = Histogram(
    "checkout_latency_seconds", "Checkout latency", buckets=(0.001, 0.003, 0.01, 0.03, 0.1, 0.3, 1)
)


class SessionRequest(BaseModel):
    userId: str | None = None


class PayRequest(BaseModel):
    outcome: Literal["success", "fail", "cancel"] = "success"


def error(http_status: int, code: str, retry_after: int | None = None, **extra) -> JSONResponse:
    headers = {"Retry-After": str(retry_after)} if retry_after else None
    return JSONResponse({"code": code, **extra}, status_code=http_status, headers=headers)


def order_body(ticket: str, status: str, **extra) -> dict:
    return {"ticket": ticket, "status": status, "statusUrl": f"/api/orders/{ticket}", **extra}


def create_app(settings: Settings = default_settings) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.store = Store(settings)
        app.state.pool = await create_pool(settings.database_url, max_size=5)
        app.state.hub = SseHub(app.state.store.redis, settings.channel, settings.max_sse_connections)
        app.state.hub.start()
        app.state.limiter = FixedWindowLimiter()
        app.state.sale_cache = (0.0, None)
        yield
        await app.state.hub.stop()
        await app.state.pool.close()
        await app.state.store.close()

    app = FastAPI(title="Flash Sale API", lifespan=lifespan, docs_url="/api/docs", openapi_url="/api/openapi.json")

    def current_user(request: Request) -> str | None:
        header = request.headers.get("authorization", "")
        token = header[7:] if header.startswith("Bearer ") else request.cookies.get("sid")
        return verify_token(settings, token)

    async def sale_state(request: Request) -> dict:
        cached_at, value = request.app.state.sale_cache
        if value is not None and time.monotonic() - cached_at < SALE_CACHE_SEC:
            return value
        value = await request.app.state.store.sale_state()
        request.app.state.sale_cache = (time.monotonic(), value)
        return value

    async def load_owned(request: Request, ticket: str) -> tuple[dict | None, JSONResponse | None]:
        user_id = current_user(request)
        if not user_id:
            return None, error(401, "UNAUTHENTICATED")
        if not TICKET.match(ticket):
            return None, error(404, "NOT_FOUND")
        reservation = await request.app.state.store.get_reservation(ticket)
        if reservation:
            if reservation["user"] != user_id:
                return None, error(404, "NOT_FOUND")
            return {"ticket": ticket, "user_id": user_id, "status": reservation["status"],
                    "expires_at": int(reservation["expires"])}, None
        row = await request.app.state.pool.fetchrow(
            "SELECT status, user_id, expires_at FROM orders WHERE order_id = $1::uuid", ticket
        )
        if row is None or row["user_id"] != user_id:
            return None, error(404, "NOT_FOUND")
        return {"ticket": ticket, "user_id": user_id, "status": row["status"],
                "expires_at": int(row["expires_at"].timestamp() * 1000)}, None

    @app.get("/healthz")
    async def healthz():
        return {"ok": True}

    @app.get("/readyz")
    async def readyz(request: Request):
        try:
            await request.app.state.store.redis.ping()
        except Exception:
            return error(503, "NOT_READY")
        return {"ok": True}

    @app.get("/internal/metrics")
    async def metrics():
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    @app.post("/api/session")
    async def create_session(response: Response, body: SessionRequest | None = None):
        wanted = body.userId if body else None
        user_id = wanted if wanted and USER_ID.match(wanted) else f"u_{secrets.token_hex(6)}"
        token = issue_token(settings, user_id)
        response.set_cookie(
            "sid", token, max_age=settings.session_ttl_sec, httponly=True, samesite="lax",
            secure=settings.cookie_secure, path="/",
        )
        return {"userId": user_id, "token": token}

    @app.get("/api/time")
    async def server_time():
        return JSONResponse({"t": time.time()}, headers={"Cache-Control": "no-store"})

    @app.get("/api/sale")
    async def sale_info(request: Request):
        sale = await sale_state(request)
        return JSONResponse(
            {
                "productId": settings.product_id,
                "name": settings.product_name,
                "priceInr": settings.product_price_inr,
                "totalStock": settings.total_stock,
                "available": sale["available"],
                "startsAt": sale["starts_at"],
                "paused": sale["paused"],
                "serverTime": int(time.time() * 1000),
                "jitterMs": settings.launch_jitter_ms,
                "reservationTtlMs": settings.reservation_ttl_ms,
            },
            headers={"Cache-Control": "no-store"},
        )

    @app.post("/api/checkout")
    async def checkout(request: Request):
        started = time.perf_counter()

        def done(result: str) -> None:
            CHECKOUT_RESULTS.labels(result).inc()
            CHECKOUT_LATENCY.observe(time.perf_counter() - started)

        try:
            user_id = current_user(request)
            if not user_id:
                done("unauthenticated")
                return error(401, "UNAUTHENTICATED")

            limiter: FixedWindowLimiter = request.app.state.limiter
            ip = request.client.host if request.client else "unknown"
            forwarded = request.headers.get("x-forwarded-for")
            if forwarded:
                ip = forwarded.split(",")[0].strip()
            if not (
                limiter.allow(f"ip:{ip}", settings.rate_ip_per_sec)
                and limiter.allow(f"user:{user_id}", settings.rate_user_per_sec)
                and limiter.allow("global", settings.admission_per_sec)
            ):
                done("rate_limited")
                return error(429, "RATE_LIMITED", retry_after=1)

            client_key = request.headers.get("idempotency-key", "")
            if not IDEMPOTENCY_KEY.match(client_key):
                done("bad_request")
                return error(400, "BAD_IDEMPOTENCY_KEY")

            sale = await sale_state(request)
            if sale["paused"]:
                done("paused")
                return error(503, "SALE_PAUSED", retry_after=5)
            now_ms = int(time.time() * 1000)
            if sale["starts_at"] is None or now_ms < sale["starts_at"]:
                done("not_started")
                return error(409, "SALE_NOT_STARTED", startsAt=sale["starts_at"], serverTime=now_ms)

            store: Store = request.app.state.store
            reservation_id = str(uuid.uuid4())
            expires_ms = now_ms + settings.reservation_ttl_ms
            idem_key = f"{user_id}:{client_key}"
            code, ticket = await store.reserve(
                user_id=user_id,
                idem_key=idem_key,
                reservation_id=reservation_id,
                now_ms=now_ms,
                expires_ms=expires_ms,
                payload={
                    "reservationId": reservation_id,
                    "productId": settings.product_id,
                    "userId": user_id,
                    "idempotencyKey": idem_key,
                    "expiresAt": expires_ms,
                },
            )
            if code == 1:
                done("reserved")
                return JSONResponse(order_body(ticket, "PROCESSING", expiresAt=expires_ms), status_code=202)
            if code == 2:
                existing = await store.get_reservation(ticket)
                status = existing["status"] if existing else "PROCESSING"
                done("replay")
                return JSONResponse(
                    order_body(ticket, status), status_code=200 if status in TERMINAL else 202
                )
            if code == 3:
                done("already_reserved")
                return error(409, "ALREADY_RESERVED", ticket=ticket)
            done("sold_out")
            return error(409, "SOLD_OUT")
        except Exception:
            log.exception("checkout failed")
            done("unavailable")
            return error(503, "TEMPORARILY_UNAVAILABLE", retry_after=1)

    @app.get("/api/me/order")
    async def my_order(request: Request):
        user_id = current_user(request)
        if not user_id:
            return error(401, "UNAUTHENTICATED")
        store: Store = request.app.state.store
        ticket = await store.ticket_for_user(user_id)
        if not ticket:
            return {"ticket": None}
        reservation = await store.get_reservation(ticket)
        status = reservation["status"] if reservation else "PROCESSING"
        expires = int(reservation["expires"]) if reservation else None
        return order_body(ticket, status, expiresAt=expires)

    @app.get("/api/orders/{ticket}")
    async def order_status(request: Request, ticket: str):
        owned, failure = await load_owned(request, ticket)
        if failure:
            return failure
        return JSONResponse(
            order_body(ticket, owned["status"], expiresAt=owned["expires_at"]), headers={"Cache-Control": "no-store"}
        )

    @app.get("/api/orders/{ticket}/events")
    async def order_events(request: Request, ticket: str):
        owned, failure = await load_owned(request, ticket)
        if failure:
            return failure
        hub: SseHub = request.app.state.hub
        if not hub.has_capacity:
            return error(503, "SSE_FULL", retry_after=2)
        return StreamingResponse(
            hub.stream(ticket, owned["status"]),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"},
        )

    @app.post("/api/orders/{ticket}/pay")
    async def pay(request: Request, ticket: str, body: PayRequest | None = None):
        owned, failure = await load_owned(request, ticket)
        if failure:
            return failure
        outcome = body.outcome if body else "success"
        store: Store = request.app.state.store
        pool = request.app.state.pool
        user_id = owned["user_id"]
        try:
            if outcome == "success":
                async with transaction(pool) as conn:
                    confirmed = await lifecycle.confirm_order(conn, ticket, user_id)
                if confirmed:
                    await store.set_status(ticket, user_id, "CONFIRMED")
                    return order_body(ticket, "CONFIRMED")
            else:
                target = "FAILED" if outcome == "fail" else "CANCELLED"
                async with transaction(pool) as conn:
                    released = await lifecycle.release_order(conn, ticket, target, user_id=user_id)
                if released:
                    await lifecycle.sync_release(store, pool, ticket, user_id, target)
                    return order_body(ticket, target)

            current = await pool.fetchval("SELECT status FROM orders WHERE order_id = $1::uuid", ticket)
            if current is None:
                return error(409, "NOT_READY", retry_after=1)
            repeat = {"success": "CONFIRMED", "fail": "FAILED", "cancel": "CANCELLED"}[outcome]
            if current == repeat:
                return order_body(ticket, current)
            return error(409, "NOT_PAYABLE", status=current)
        except Exception:
            log.exception("payment transition failed")
            return error(503, "TEMPORARILY_UNAVAILABLE", retry_after=2)

    return app


app = create_app()
