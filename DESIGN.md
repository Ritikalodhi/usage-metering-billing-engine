# Design — Usage Metering & Billing Engine

**Stack:** Python 3.11 · FastAPI · PostgreSQL (Docker) · SQLAlchemy + Alembic · Stripe test mode
**Non-goal (explicit):** no invoicing, no proration, no overage billing in core. Usage over quota is *rejected*, never billed.

## 1. Problem

A multi-tenant SaaS backend that answers three questions per tenant: how much has been used, what does it cost, and is the tenant over its plan limit. Correctness under retries and duplicate webhooks is the primary requirement; feature surface is deliberately tiny.

## 2. Plans & quotas

| Plan | API calls / month | AI tokens / month |
|---|---|---|
| Free | 1,000 | 100,000 |
| Pro  | 50,000 | 5,000,000 |

A "month" is the UTC calendar month (`date_trunc('month', now())`). Chosen over anniversary-based cycles because it makes rollups a single indexed range scan and removes an entire class of off-by-one bugs. Documented as a limitation.

## 3. Money math

All money is stored as **integer micro-cents** (`1 cent = 1_000_000 µ¢`). No floats anywhere, including in JSON responses — the API returns `cost_micro_cents` (int) and a display-only `cost_usd` string rendered with `Decimal` at the edge.

Pricing constants, pinned in `app/config/pricing.py`:

| Category | Rate | µ¢ per token |
|---|---|---|
| `input` | $3.00 / 1M | 300 |
| `cached_input` | $0.30 / 1M | 30 |
| `output` | $15.00 / 1M | 1500 |
| `reasoning` | billed **as output** | 1500 |
| API call | $0.002 / call | 200 |

Rules encoded, not assumed:
- cached input is a **separate category at a separate rate** — never folded into `input`
- reasoning tokens are **added to the output bucket** before pricing, and are not free
- `total = Σ(qty_category × rate_category)` per category, summed last — categories are never added together before pricing

```
cost = in*300 + cached*30 + (out + reasoning)*1500
```

Quota consumption counts **all four token categories at face value** (cheaper ≠ free). This is a judgement call and is stated in the README.

## 4. Idempotency strategy (the heart of it)

Every billable request carries `Idempotency-Key` (header, required). Guarantee: *same tenant + same key = exactly one usage event, and the second response byte-mirrors the first.*

Mechanism — one table, one unique constraint, one transaction:

1. `INSERT INTO idempotency_keys (tenant_id, key, request_hash, state='in_progress') ON CONFLICT DO NOTHING`
2. **0 rows inserted** → a record exists:
   - `state='completed'` → return stored `response_body` + `response_status`, `Idempotency-Replayed: true`. No new usage event.
   - `state='in_progress'` → `409 Conflict` (request still in flight)
   - `request_hash` differs → `422` (key reused with a different payload)
3. **1 row inserted** → we own it. In the *same* transaction: insert `usage_events`, compute cost, then `UPDATE idempotency_keys SET state='completed', response_body=…`.

Correctness rests on the DB unique index `(tenant_id, key)`, not on application-level read-then-write. Two concurrent retries: one wins the insert, the other takes the conflict branch. Single commit means we can never have a usage event without its key record, or vice versa.

## 5. Quota enforcement & boundary rule

Documented rule: **a request is allowed only if `used + requested <= limit`.** A request that would cross the limit is rejected *in full* — no partial metering. So at 999/1000, a 1-call request succeeds (→1000); the next returns 429. A request for 5 calls at 998 is rejected outright, and usage stays 998.

The check and the insert happen in one transaction; the rollup is computed after taking `SELECT … FOR NO KEY UPDATE` on the tenant row to serialize concurrent metering for the same tenant (SQLAlchemy: `with_for_update(key_share=True)`).

**Why `FOR NO KEY UPDATE` and not `FOR UPDATE` (decision record).** Step 1 of the metering path (`claim()`) inserts into `idempotency_keys`, which has a foreign key to `tenants`. Postgres takes an automatic `FOR KEY SHARE` lock on the referenced tenant row for that insert. Plain `FOR UPDATE` conflicts with `FOR KEY SHARE`, so two concurrent requests for the same tenant each hold a key-share lock and each wait to upgrade, which produces `DeadlockDetected`. This was reproduced in `test_concurrency_race_at_limit`. `FOR NO KEY UPDATE` is compatible with `FOR KEY SHARE` but still conflicts with itself, so concurrent metering for one tenant is still fully serialized. Rejected alternative: taking the tenant lock before `claim()`, which would force every replayed or duplicate request through the exclusive lock before it can short-circuit.

Plan changes serialize with metering: a plain `UPDATE tenants SET plan_id=…` (the Phase 5 webhook) takes `FOR NO KEY UPDATE` itself, because `plan_id` is not a key column. A webhook plan flip therefore waits for an in-flight metering transaction, and the next metering transaction sees the new plan.

| Condition | Status | Body |
|---|---|---|
| usage limit reached, plan is active | `429` + `Retry-After: <secs to month rollover>` | `{code:"quota_exceeded", used, limit, requested}` |
| subscription `past_due`/`canceled`/`unpaid` | `402` | `{code:"payment_required", …}` |
| Free tenant asking for a Pro-only volume | `402` | `{code:"upgrade_required", …}` |

429 = "you've used your allowance." 402 = "your billing state forbids this." Quota exhaustion on an otherwise healthy plan is never 402.

## 6. API surface

| Method | Path | Notes |
|---|---|---|
| `POST` | `/v1/generate` | the dummy billable endpoint. `Idempotency-Key` required. Body: `{input_tokens, cached_input_tokens, output_tokens, reasoning_tokens}` (simulated). Meters 1 API call + the tokens. |
| `GET` | `/v1/usage` | rollup: `{plan, period, api_calls:{used,limit}, tokens:{used,limit,by_category}, cost_micro_cents, cost_usd}` |
| `POST` | `/v1/billing/checkout` | creates a Stripe Checkout session for Pro, returns `url` |
| `POST` | `/webhooks/stripe` | raw body, signature-verified |
| `GET` | `/healthz` | liveness |

Auth: `Authorization: Bearer <api_key>` → resolves to exactly one tenant. Tenant id is **never** taken from the request body; every query is scoped by the resolved tenant.

## 7. Layers

```
api/        FastAPI routers · Pydantic schemas · auth dep · error handlers  (HTTP only)
services/   MeterService · QuotaService · CostCalculator · StripeSyncService (no HTTP, no SQL text)
repos/      SQLAlchemy queries, one per aggregate                            (no business rules)
db/         models · Alembic migrations
config/     pricing.py · plans.py · settings.py (env only)
jobs/       rollup_refresh (background), reconcile (stretch)
```
Validation lives at the boundary: Pydantic rejects negative/oversized token counts → `422`, never a 500. Services raise typed domain errors (`QuotaExceeded`, `PaymentRequired`, `DuplicateRequest`) that a single exception handler maps to status codes.

## 8. Webhook handling

Events: `checkout.session.completed`, `customer.subscription.updated`, `customer.subscription.deleted`.

1. Read the **raw** body (no Pydantic, no re-serialization) → `stripe.Webhook.construct_event(payload, sig_header, whsec)`. Failure → `400`, nothing written.
2. `INSERT INTO webhook_events (stripe_event_id) ON CONFLICT DO NOTHING` → 0 rows means replay → `200`, no state change.
3. Apply the change to `subscriptions` + `tenants.plan_id` in the same transaction, keyed off `stripe_subscription_id`.
4. Ignore events whose `created` is older than the currently stored subscription snapshot (Stripe does not guarantee ordering).

Stripe is the source of truth for payment state; the DB is a mirror updated only through verified events.

## 9. Background job

`rollup_refresh` (APScheduler, every 5 min + on demand): recomputes `usage_rollups` per tenant/period from `usage_events`, so `GET /usage` is a single-row read under load. Retries with exponential backoff, 3 attempts, then writes a `job_failures` row and logs at ERROR. The rollup is a cache — it is always rebuildable from `usage_events`, which remain the ledger.

## 10. Secrets

`STRIPE_API_KEY`, `STRIPE_WEBHOOK_SECRET`, `DATABASE_URL` from env only, loaded via pydantic-settings. `.env` git-ignored from commit #1; `.env.example` ships placeholders. Secret values are never logged — the log formatter redacts any string matching `sk_|whsec_`.

## 11. Known limitations (to repeat in README)

- Calendar-month periods, not billing-anniversary periods.
- Quota counts cached tokens at face value even though they cost less.
- Over-quota requests are rejected, not billed as overage (stretch goal).
- Idempotency keys are retained 30 days, then pruned; a retry after that would double-count.
- Metering is judged against the plan row as of its own lock acquisition; a plan flip that commits after that point applies to the next request, not the one in flight.
- The usage ledger records reasoning tokens under their own `reasoning` category; pricing folds them into the output rate. Quota counts all four token categories at face value.