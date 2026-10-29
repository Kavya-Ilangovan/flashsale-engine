CREATE TABLE IF NOT EXISTS inventory (
    product_id      BIGINT PRIMARY KEY,
    name            TEXT NOT NULL,
    price_inr       INT  NOT NULL,
    total_stock     INT  NOT NULL CHECK (total_stock >= 0),
    available_stock INT  NOT NULL CHECK (available_stock >= 0),
    CHECK (available_stock <= total_stock)
);

CREATE TABLE IF NOT EXISTS orders (
    order_id        UUID PRIMARY KEY,
    product_id      BIGINT NOT NULL REFERENCES inventory (product_id),
    user_id         TEXT   NOT NULL,
    idempotency_key TEXT   NOT NULL UNIQUE,
    status          TEXT   NOT NULL CHECK (status IN
                      ('PAYMENT_PENDING', 'CONFIRMED', 'FAILED', 'EXPIRED', 'CANCELLED', 'REJECTED')),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at      TIMESTAMPTZ NOT NULL,
    redis_synced    BOOLEAN NOT NULL DEFAULT TRUE
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_orders_active_user
    ON orders (product_id, user_id) WHERE status IN ('PAYMENT_PENDING', 'CONFIRMED');
CREATE INDEX IF NOT EXISTS ix_orders_expiry
    ON orders (expires_at) WHERE status = 'PAYMENT_PENDING';
CREATE INDEX IF NOT EXISTS ix_orders_unsynced
    ON orders (updated_at) WHERE redis_synced = FALSE;

CREATE TABLE IF NOT EXISTS order_events (
    id          BIGSERIAL PRIMARY KEY,
    order_id    UUID NOT NULL,
    from_status TEXT,
    to_status   TEXT NOT NULL,
    detail      JSONB,
    at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_order_events_order ON order_events (order_id);
