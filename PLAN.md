# Implementation Plan — Usage Metering & Billing Engine

Python 3.11 · FastAPI · Postgres (Docker) · SQLAlchemy 2.0 + Alembic · Stripe test mode.
Every step ends with something runnable and a line you can paste into `EVIDENCE.md`. Build in this order — each phase depends on the one before it.

---

## Phase 0 — Repo skeleton (≈1 h)

Create the public repo `flyrank-capstone-metering-billing`. **First commit contains `.gitignore` with `.env` in it.** Not the second commit.

```
.
├── app/
│   ├── main.py                 # FastAPI app, router mounting, exception handlers
│   ├── api/
│   │   ├── deps.py             # auth dependency -> Tenant
│   │   ├── errors.py           # domain error -> HTTP status mapping
│   │   ├── schemas.py          # Pydantic request/response models
│   │   └── routes/
│   │       ├── generate.py  billing.py  usage.py  webhooks.py  health.py
│   ├── services/
│   │   ├── meter.py  quota.py  cost.py  stripe_sync.py
│   ├── repos/
│   │   ├── tenants.py  usage.py  idempotency.py  subscriptions.py  webhooks.py
│   ├── db/
│   │   ├── models.py  session.py
│   ├── config/
│   │   ├── settings.py  pricing.py  plans.py
│   └── jobs/
│       ├── scheduler.py  rollup_refresh.py  prune_idempotency.py
├── migrations/                 # alembic
├── tests/
├── scripts/seed.py
├── docker-compose.yml
├── capstone.yaml
├── .env.example
└── README.md  EVIDENCE.md  BUILDLOG.md  DESIGN.md
```

`docker-compose.yml`: `postgres:16` + the app, one `docker compose up` boots both. `depends_on` with a healthcheck so the app doesn't race the DB.

**Gate:** `docker compose up` → `GET /healthz` returns `{"status":"ok"}` with a real DB round-trip (`SELECT 1`), not a hardcoded string.

---

## Phase 1 — Data layer (≈3 h)

1. Translate `0001_initial.sql` into `app/db/models.py` (SQLAlchemy 2.0 typed `Mapped[]` style), then `alembic revision --autogenerate` and **diff the output against the hand-written SQL**. Autogenerate misses enums, partial indexes, and CHECK constraints — hand-patch them in.
2. `config/plans.py` and `config/pricing.py` hold the constants as frozen dataclasses. Pricing is loaded from code, never from the DB, so a totals proof is reproducible from a git SHA.
3. `scripts/seed.py`: creates 2 tenants (one Free, one Pro), prints their plaintext API keys. Idempotent — safe to re-run.

**Gate:** `alembic upgrade head && python scripts/seed.py` on an empty DB prints two usable API keys.
**Evidence:** paste the seed output + `\d usage_events`.

---

## Phase 2 — Cost calculator (≈2 h)

Pure functions, zero I/O. Build this before the metering path — it's the easiest thing to prove correct and the metering path consumes it.

```python
def price_tokens(input=0, cached_input=0, output=0, reasoning=0) -> TokenCost:
    # reasoning is folded into the OUTPUT bucket, then priced.
    # cached_input keeps its own rate. Categories are priced separately,
    # summed last -- never added together before pricing.
```

Return a per-category breakdown, not just a total, so `/usage` can show its work.

Write these tests now (they're the Probe 5 evidence):
- all-input vs same count all-cached → cached is exactly 10% of the cost
- reasoning tokens priced identically to output tokens, one-for-one
- the naive bug: `(in+cached+out+reasoning) × input_rate` ≠ your total — assert the difference
- a worked example with hand-computed µ¢ in the test as a literal

**Gate:** `pytest tests/test_cost.py` green.
**Evidence:** test names + output, plus the worked example table in `EVIDENCE.md`.

---

## Phase 3 — Idempotency + metering (≈6 h) ← the heart

Order matters: idempotency wrapper first, business logic second.

**Step 1 — `repos/idempotency.py`.** One function: `claim(tenant_id, key, endpoint, request_hash) -> Claim`, implementing the `INSERT … ON CONFLICT DO NOTHING` branch from the design. Returns `OWNED` / `REPLAY(body,status)` / `IN_PROGRESS` / `HASH_MISMATCH`.

**Step 2 — `services/meter.py`.** `record()` runs in a single transaction:
```
claim key  ->  not OWNED? short-circuit
SELECT tenant FOR UPDATE           (serializes same-tenant concurrency)
rollup current period usage
quota check (used + requested <= limit)
price the usage
INSERT usage_events (1 api_call row + 1 row per non-zero token category)
UPDATE idempotency_keys SET state='completed', response_body=...
COMMIT
```
Rejections still complete the key with their 4xx body — a retried over-quota request must replay the same 429, not re-evaluate.

**Step 3 — `POST /v1/generate`.** `Idempotency-Key` header required (missing → 400). Pydantic validates token counts as `conint(ge=0, le=10_000_000)`. Response echoes `usage_event_ids`, the cost breakdown, and remaining quota. Replays add `Idempotency-Replayed: true`.

**Step 4 — quota responses.** `429` + `Retry-After` for exhausted quota, `402` for bad subscription status. One exception handler maps `QuotaExceeded`/`PaymentRequired` — routes never build error bodies themselves.

Tests to write (these are Probes 1 and 2):
- same key twice → one row in `usage_events`, identical response bodies
- **concurrent** double-send (two threads / `asyncio.gather`) → still one row. This is the test that catches a read-then-write implementation.
- same key, different body → 422
- walk a Free tenant 998 → 999 → 1000 → 1001, assert status at each step
- a 5-call request at 998 → rejected, usage still 998
- over-quota request retried with the same key → identical 429

**Gate:** all of the above green; `SELECT count(*) FROM usage_events` confirms by hand.
**Evidence:** curl transcript of the double-send + the row count, and the boundary walk.

---

## Phase 4 — Rollups and `GET /usage` (≈2 h)

`services/quota.py` computes the rollup from `usage_events` with a single grouped query over `(tenant_id, occurred_at >= date_trunc('month', now()))`. `GET /v1/usage` returns plan, period, used/limit per meter, per-category token breakdown, `cost_micro_cents`, and `cost_usd` as a string.

Write the read against `usage_events` **first**, get it correct, and only then add the `usage_rollups` cache in Phase 6 — with a test asserting cache == live query.

**Gate:** `/usage` totals match the Phase 2 calculator for a seeded set of events.
**Evidence:** curl output of `/usage` next to the hand-computed expected total.

---

## Phase 5 — Stripe (≈8 h)

**Step 1 — setup.** Stripe test-mode dashboard → create a "Pro" product + recurring price → `STRIPE_PRICE_PRO` in `.env`. `stripe login`, then `stripe listen --forward-to localhost:8000/webhooks/stripe` — it prints a `whsec_…`, which goes in `.env` too. **Never in a commit.**

**Step 2 — `POST /v1/billing/checkout`.** Create-or-reuse a Stripe Customer for the tenant (store `stripe_customer_id`), then a Checkout Session in `subscription` mode with `client_reference_id = tenant_id` and `metadata.tenant_id`. Return the URL. Pay with `4242 4242 4242 4242`, any future expiry.

**Step 3 — `POST /webhooks/stripe`.** In strict order:
1. `await request.body()` — the **raw** bytes. If you let Pydantic touch it, signature verification fails and you'll lose an hour. Common trap.
2. `stripe.Webhook.construct_event(raw, sig_header, whsec)` — any exception → `400`, no writes.
3. `INSERT INTO webhook_events (...) ON CONFLICT DO NOTHING` → 0 rows → return `200` immediately, no state change.
4. Dispatch on type; ignore events older than `subscriptions.last_event_at`.
5. Update `subscriptions` + `tenants.plan_id` in one transaction, then stamp `processed_at`.

Handlers:
- `checkout.session.completed` → fetch the subscription, upsert, set tenant plan to Pro
- `customer.subscription.updated` → sync status + period; `past_due`/`unpaid` is what later produces 402s
- `customer.subscription.deleted` → status `canceled`, tenant back to Free

Tests (Probes 3 and 4):
- forged signature (`Stripe-Signature: t=1,v1=deadbeef`) → 400, DB unchanged
- `stripe trigger checkout.session.completed` twice → `webhook_events` has 1 row, plan flipped once
- full Checkout in a browser → tenant goes Free → Pro, `/usage` shows the new limits

**Gate:** a real test Checkout flips a tenant via webhook.
**Evidence:** `stripe listen` log lines, the forged-signature 400, before/after `/usage`.

---

## Phase 6 — Background job + hardening (≈4 h)

- **`rollup_refresh`** (APScheduler, 5 min): recompute `usage_rollups` for tenants with events since `refreshed_at`. Exponential backoff, 3 attempts, then a `job_failures` row + ERROR log. Point `/usage` at the cache with a fallback to the live query. Test: cache equals live query after N random events.
- **`prune_idempotency`** (daily): delete keys older than 30 days.
- **Log redaction:** a formatter filter that masks anything matching `sk_[A-Za-z0-9]+|whsec_[A-Za-z0-9]+`. Test it.
- **Global handlers:** `RequestValidationError` → 422 with a clean body; a catch-all that logs the traceback and returns a generic 500 with a request id. No stack traces in responses.
- Request-id middleware, echoed in every log line and response header.

**Gate:** kill the scheduler mid-run, restart, rollups still correct (they're derived — that's the point).

---

## Phase 7 — Submission pack (≈3 h)

- **`README.md`** — what it does, ASCII architecture diagram, `docker compose up` + seed steps verified **on a clean clone into a fresh directory**, your Pro plan numbers, the boundary rule stated explicitly, and the limitations list from `DESIGN.md`.
- **`capstone.yaml`** — `run:`, `seed:`, `test:`, `base_url:`, endpoints to probe.
- **`EVIDENCE.md`** — one proof per Section 6 checkbox. Fill it as you go, not at the end; the transcripts are hard to reconstruct later.
- **`BUILDLOG.md`** — where AI helped, where it was wrong, what you changed. Write entries the day they happen. Note at least a few places AI was actually wrong — an all-praise log reads as fiction, and the evaluator can ask you about any 2–3 lines.
- **`.env.example`** — every var, placeholder values.

Final: `git log --oneline` should show the phases. Clone the repo into `/tmp` and run it yourself before submitting.

---

## Effort summary

| Phase | Hours |
|---|---|
| 0 skeleton | 1 |
| 1 data layer | 3 |
| 2 cost calculator | 2 |
| 3 idempotency + metering | 6 |
| 4 rollups + /usage | 2 |
| 5 Stripe | 8 |
| 6 job + hardening | 4 |
| 7 submission pack | 3 |
| **Total** | **~29 h** (+ buffer → the brief's 30–45) |

Stretch goals only after every Section 6 box is green. If you take one, take **reconciliation** — a nightly diff of your `subscriptions` against Stripe's list, reporting drift. It's ~3 hours, it directly demonstrates that you understand webhooks can be missed, and it's the one that tends to impress interviewers most.

## Traps that cost people the most time

1. Parsing the webhook body before verifying the signature → verification always fails.
2. Read-then-write idempotency → passes sequential tests, double-counts under concurrency.
3. Floats for money → a totals proof that's off by a cent and can't be explained.
4. `Retry-After` omitted on 429 → a reviewer notices.
5. Committing `.env` in the first commit → repo-hygiene fail, and rewriting history is worse than avoiding it.
