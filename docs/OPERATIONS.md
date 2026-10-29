# Operations and Deployment Guide

## 1. Components

| Component | Process | Scaling |
|---|---|---|
| Gateway | nginx (serves static build, proxies `/api`) | Horizontal behind a load balancer or CDN |
| API | `uvicorn flashsale.api:app` | Horizontal; stateless; 3 replicas in the reference compose file |
| Relay | `python -m flashsale.relay` | 1 to 2 replicas (duplicates are harmless) |
| Worker | `python -m flashsale.worker` | 2+ replicas; concurrency bounded by `WORKER_CONCURRENCY` |
| Redis | Single instance, AOF `appendfsync always` | See Section 6 |
| PostgreSQL | Single primary | Put a connection pooler in front for many replicas |
| RabbitMQ | Single node (durable queues) | Clustered/quorum queues for production |

## 2. Configuration

All settings are environment variables (see `.env.example`).

| Variable | Default | Purpose |
|---|---|---|
| `REDIS_URL`, `DATABASE_URL`, `AMQP_URL` | local services | Connection strings |
| `SESSION_SECRET` | dev value | HMAC key for session tokens. **Must be changed** |
| `COOKIE_SECURE` | `0` | Set to `1` when served over HTTPS |
| `PRODUCT_ID`, `TOTAL_STOCK` | `42`, `500` | Product and units for sale |
| `SALE_START_DELAY_SEC` / `SALE_START_AT_MS` | `45` / unset | Sale start (relative or absolute epoch ms) |
| `RESERVATION_TTL_MS` | `600000` | Payment window |
| `LAUNCH_JITTER_MS` | `0` | Client-side random delay for fairness (0 keeps strict first-click) |
| `RATE_USER_PER_SEC`, `RATE_IP_PER_SEC` | `5`, `50` | Per-replica limits |
| `ADMISSION_PER_SEC_PER_REPLICA` | `0` | Global admission cap per replica (0 disables) |
| `WORKER_CONCURRENCY`, `MAX_RETRIES` | `8`, `5` | Worker parallelism and retry budget |
| `PENDING_GRACE_MS` | `30000` | Grace before an unpersisted reservation is released |

## 3. Deployment

### 3.1 Local development (Docker infrastructure, Python on the host)

```powershell
docker compose up -d postgres redis rabbitmq
cd backend
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements-dev.txt
copy ..\.env.example .env
python -m flashsale.bootstrap --reset
uvicorn flashsale.api:app --port 3000          # terminal 1
python -m flashsale.relay                      # terminal 2
python -m flashsale.worker                     # terminal 3
```

### 3.2 Full stack (Docker)

```powershell
copy .env.example .env
docker compose --profile app up --build
```

The sale page is served at `http://localhost:8080`. To start a fresh sale:
`docker compose --profile app run --rm init python -m flashsale.bootstrap --reset`.

### 3.3 Production outline

1. Build and push two images: `backend` (API, relay, worker share one image with different commands) and `gateway`
   (static build plus nginx). The gateway image build fails if the front-end exceeds the size budget.
2. Provision managed or replicated PostgreSQL, Redis (persistence enabled) and RabbitMQ (quorum queues).
3. Run `python -m flashsale.bootstrap` once per environment as a release step (it is idempotent and never overwrites
   a live Redis counter).
4. Deploy API (3 or more), relay (1 to 2) and worker (2 or more) with readiness probe `GET /readyz` and liveness probe
   `GET /healthz`.
5. Place a CDN with WAF and bot management in front of the gateway. Serve `/` and `/assets/*` from the CDN; configure
   nginx `real_ip` so per-IP limits use the client address.
6. Terminate TLS at the CDN or load balancer; set `COOKIE_SECURE=1`.
7. Replace the demonstration authentication and payment endpoints (see `docs/API_REFERENCE.md`).

## 4. Pre-sale checklist

- Scale API and workers to the planned size **before** 20:00 (scheduled scaling; autoscaling reacts too slowly for a
  ten-second burst).
- Run `loadtest/k6-flash-sale.js` against a production-like environment; set rate limits and admission rate from the
  measured capacity.
- Confirm Redis persistence (`appendonly yes`, `appendfsync always`) and free memory.
- Confirm `redis-cli GET {p42}:stock` equals the units for sale and `GET {p42}:start` is the intended start time.
- Confirm queue depth is zero and the dead-letter queue is empty.
- Rehearse the pause switch and the incident runbook.

## 5. Runbook

| Situation | Action |
|---|---|
| Pause the sale | `redis-cli SET {p42}:paused 1` (API answers `503 SALE_PAUSED`); `DEL` to resume |
| New sale | `python -m flashsale.bootstrap --reset` (wipes orders, Redis state and queues) |
| Dead-lettered messages | Inspect `checkout.dlq`, fix the cause, shovel messages back to `checkout.reserve`; processing is idempotent |
| Redis restarted without data | The sale fails closed; the reconciler rebuilds the counter from PostgreSQL once queues drain |
| Suspected drift | Check gauges `reconcile_drift` and `db_invariant_violation`; compare `orders` with `order_events` |
| Worker backlog | Increase worker replicas and, carefully, `WORKER_CONCURRENCY`; watch PostgreSQL connections |

## 6. Production hardening notes

- **Redis high availability.** Use a replicated deployment with automatic failover. Asynchronous replication can lose
  the most recent writes on failover; either issue `WAIT 1 <timeout>` after the reservation write or rely on the
  reconciler, which releases holds that never reached PostgreSQL. The key layout already uses a hash tag so that all
  keys of a product share one Redis Cluster slot.
- **PostgreSQL.** Use a connection pooler (for example PgBouncer in transaction mode) and size `max_connections`
  for workers and APIs. The worker transaction is short and touches one inventory row.
- **RabbitMQ.** Use quorum queues and publisher confirms (already enabled).
- **Secrets.** Provide `SESSION_SECRET` and database credentials from a secret manager, not from `.env` files.

## 7. Monitoring

| Signal | Source |
|---|---|
| Request rate, error rate, latency percentiles | `checkout_requests_total`, `checkout_latency_seconds` (API `/internal/metrics`) |
| Business metrics | `worker_events_total{outcome}`; sold-out and expiry counts |
| Correctness | `db_invariant_violation`, `reconcile_drift` (worker metrics, port 9101) |
| Queue health | RabbitMQ management: depth, oldest message age, DLQ size |
| Capacity | Redis latency and memory, PostgreSQL pool saturation and lock waits, gateway 429/503 rates |

Alert on: any non-zero `db_invariant_violation` or sustained `reconcile_drift`, DLQ growth, queue age beyond a few
seconds, elevated 5xx, Redis memory or latency, database pool saturation.

## 8. Security checklist

- Change `SESSION_SECRET`; use HTTPS and `COOKIE_SECURE=1`; cookies are `HttpOnly` and `SameSite=Lax`.
- Restrict `/internal/*` (the reference gateway returns 404 for it); do not expose RabbitMQ, Redis or PostgreSQL.
- Replace RabbitMQ default credentials; enable Redis authentication in shared environments.
- Keep a managed WAF and bot service in front of the CDN; the gateway filter is a baseline only.
- Order lookups return `404` for tickets not owned by the caller to prevent enumeration.
