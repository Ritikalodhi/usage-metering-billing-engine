-- 0001_initial.sql — Usage Metering & Billing Engine
-- Money is stored as integer micro-cents (1 cent = 1_000_000). No floats, anywhere.

CREATE TYPE plan_code        AS ENUM ('free', 'pro');
CREATE TYPE usage_type       AS ENUM ('api_call', 'tokens');
CREATE TYPE token_category   AS ENUM ('input', 'cached_input', 'output', 'reasoning');
CREATE TYPE sub_status       AS ENUM ('active','trialing','past_due','canceled','unpaid','incomplete');
CREATE TYPE idem_state       AS ENUM ('in_progress','completed');

-- ---------------------------------------------------------------- plans
CREATE TABLE plans (
    id                  SMALLSERIAL PRIMARY KEY,
    code                plan_code    NOT NULL UNIQUE,
    display_name        TEXT         NOT NULL,
    api_call_limit      BIGINT       NOT NULL CHECK (api_call_limit >= 0),
    token_limit         BIGINT       NOT NULL CHECK (token_limit >= 0),
    stripe_price_id     TEXT,                       -- null for free
    created_at          TIMESTAMPTZ  NOT NULL DEFAULT now()
);

INSERT INTO plans (code, display_name, api_call_limit, token_limit) VALUES
    ('free', 'Free', 1000,  100000),
    ('pro',  'Pro',  50000, 5000000);

-- -------------------------------------------------------------- tenants
CREATE TABLE tenants (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name          TEXT        NOT NULL,
    plan_id       SMALLINT    NOT NULL REFERENCES plans(id),
    -- api_key is stored hashed; the plaintext is shown once at creation.
    api_key_hash  TEXT        NOT NULL UNIQUE,
    stripe_customer_id TEXT   UNIQUE,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_tenants_stripe_customer ON tenants(stripe_customer_id);

-- --------------------------------------------------------- subscriptions
-- Mirror of Stripe state. Written only by verified webhook events.
CREATE TABLE subscriptions (
    id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id               UUID        NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    plan_id                 SMALLINT    NOT NULL REFERENCES plans(id),
    stripe_subscription_id  TEXT        NOT NULL UNIQUE,
    status                  sub_status  NOT NULL,
    current_period_start    TIMESTAMPTZ,
    current_period_end      TIMESTAMPTZ,
    -- Stripe does not guarantee webhook ordering: drop events older than this.
    last_event_at           TIMESTAMPTZ NOT NULL,
    cancel_at_period_end    BOOLEAN     NOT NULL DEFAULT FALSE,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX idx_sub_one_active_per_tenant
    ON subscriptions(tenant_id)
    WHERE status IN ('active','trialing','past_due');

-- --------------------------------------------------- idempotency_keys
-- The exactly-once guarantee lives on this unique index, not in app code.
CREATE TABLE idempotency_keys (
    id              BIGSERIAL PRIMARY KEY,
    tenant_id       UUID        NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    key             TEXT        NOT NULL,
    endpoint        TEXT        NOT NULL,
    request_hash    TEXT        NOT NULL,   -- sha256 of canonical body; mismatch => 422
    state           idem_state  NOT NULL DEFAULT 'in_progress',
    response_status SMALLINT,
    response_body   JSONB,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at    TIMESTAMPTZ,
    CONSTRAINT uq_idem_tenant_key UNIQUE (tenant_id, key)
);
CREATE INDEX idx_idem_created_at ON idempotency_keys(created_at);  -- for the 30d pruner

-- -------------------------------------------------------- usage_events
-- The ledger. Append-only; never updated, never deleted.
CREATE TABLE usage_events (
    id               BIGSERIAL PRIMARY KEY,
    tenant_id        UUID           NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    idempotency_id   BIGINT         NOT NULL REFERENCES idempotency_keys(id),
    type             usage_type     NOT NULL,
    category         token_category,              -- null iff type = 'api_call'
    quantity         BIGINT         NOT NULL CHECK (quantity > 0),
    unit_price_micro_cents BIGINT   NOT NULL CHECK (unit_price_micro_cents >= 0),
    cost_micro_cents BIGINT         NOT NULL CHECK (cost_micro_cents >= 0),
    occurred_at      TIMESTAMPTZ    NOT NULL DEFAULT now(),
    CONSTRAINT ck_category_matches_type CHECK (
        (type = 'api_call' AND category IS NULL) OR
        (type = 'tokens'   AND category IS NOT NULL)
    )
);
-- The hot path: rollup for one tenant over one month.
CREATE INDEX idx_usage_tenant_time ON usage_events(tenant_id, occurred_at DESC);
CREATE INDEX idx_usage_tenant_type_time ON usage_events(tenant_id, type, occurred_at DESC);

-- ------------------------------------------------------ usage_rollups
-- Derived cache, always rebuildable from usage_events. period = first day of UTC month.
CREATE TABLE usage_rollups (
    tenant_id        UUID        NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    period           DATE        NOT NULL,
    api_calls_used   BIGINT      NOT NULL DEFAULT 0,
    tokens_used      BIGINT      NOT NULL DEFAULT 0,
    cost_micro_cents BIGINT      NOT NULL DEFAULT 0,
    by_category      JSONB       NOT NULL DEFAULT '{}'::jsonb,
    refreshed_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, period)
);

-- ------------------------------------------------------ webhook_events
-- Stripe replay protection: the insert either wins or the event is a duplicate.
CREATE TABLE webhook_events (
    stripe_event_id  TEXT        PRIMARY KEY,
    type             TEXT        NOT NULL,
    payload          JSONB       NOT NULL,
    received_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    processed_at     TIMESTAMPTZ
);

-- -------------------------------------------------------- job_failures
CREATE TABLE job_failures (
    id          BIGSERIAL PRIMARY KEY,
    job_name    TEXT        NOT NULL,
    attempts    SMALLINT    NOT NULL,
    error       TEXT        NOT NULL,
    failed_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
