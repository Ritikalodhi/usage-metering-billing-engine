# EVIDENCE.md

One pasted proof per Requirements checkbox (Section 6 of the brief). Filled in as each phase completes.
Items marked **[PASTE]** need real terminal output from your machine before submission. Do not leave placeholders in the final repo.

---

## Phase 0: App skeleton / healthz

```text
$ curl -i localhost:8000/healthz
HTTP/1.1 200 OK
{"status":"ok"}
```

---

## Phase 1: Data model, migrations, seed
**Requirement: Database includes tenants, plans, subscriptions, usage events; isolated per tenant**

```
$ docker compose exec app alembic upgrade head
$ docker compose exec app python scripts/seed.py
tenant name=seed-free plan=free
tenant name=seed-pro  plan=pro
```

Schema checked with `\d` against 0001_initial.sql:
- `usage_events`: ck_category_matches_type, ck_usage_events_cost, ck_usage_events_quantity, ck_usage_events_unit_price present
- `idempotency_keys`: uq_idem_tenant_key UNIQUE CONSTRAINT on (tenant_id, key)
- `subscriptions`: idx_sub_one_active_per_tenant partial UNIQUE index with `WHERE status = ANY(...)`

Seed idempotency: re-ran seed.py repeatedly, tenants stayed at 2 rows. Fixed by `INSERT ... ON CONFLICT (api_key_hash) DO NOTHING`, replacing an earlier SELECT-then-INSERT. The extra third row was `test-tenant-*`, created by tests/test_data_layer.py writing to the app database (test-isolation issue, tracked in BUILDLOG.md).

---

## Phase 2: Cost calculation
**Requirement: AI token pricing handles cached input, reasoning, output; constants pinned in config**

```
$ docker compose exec app pytest tests/test_cost.py -v
5 passed in 0.70s
- test_cached_input_rate_is_ten_percent_of_normal_input_rate
- test_reasoning_tokens_priced_identically_to_output_tokens
- test_regression_naive_sum_times_input_rate_differs_from_total
- test_worked_example_literal_micro_cents
- test_price_api_call
```

Worked example (all integer micro-cents):

| Category | Quantity | Rate (µ¢/token) | Cost (µ¢) |
|---|---|---|---|
| input | 1,000 | 300 | 300,000 |
| cached_input | 500 | 30 | 15,000 |
| output + reasoning (folded) | 200 + 100 | 1,500 | 450,000 |
| **Total** | | | **765,000** |

The naive calculation `(1000+500+200+100) * 300 = 540,000` differs from 765,000, and a test asserts this.

Implementation: `app/services/cost.py`. Constants only: `app/config/pricing.py`.

---

## Phase 3a: Idempotency claim mechanism
**Requirement: exactly one usage event under retries, deduplicated by idempotency key**

```
$ docker compose exec app pytest tests/test_idempotency.py -v
5 passed in 5.11s   (platform linux, container)
- test_first_claim_fresh_key
- test_claim_replay_after_complete
- test_claim_hash_mismatch
- test_claim_in_progress
- test_concurrency_claims
```

`test_concurrency_claims`: 12 real threads via ThreadPoolExecutor, each with its own `SessionLocal()`, released together by `threading.Barrier(12)`. Result: exactly 1 `OWNED`, 11 `IN_PROGRESS`.

Mechanism: `INSERT ... ON CONFLICT (tenant_id, key) DO NOTHING RETURNING id` (`app/repos/idempotency.py`). Uniqueness is enforced by the database, not by read-then-write.

---

## Phase 3b: MeterService and quota enforcement
**Requirements: Metering; Quotas (429/402 with clear message) — Section 6**

```
$ docker compose exec app pytest tests/test_meter.py -v
6 passed (container, platform linux)
- test_normal_request_metering
- test_idempotency_replay_metering
- test_boundary_walk
- test_rejection_in_full_no_partial_metering
- test_subscription_past_due
- test_concurrency_race_at_limit
```

Boundary rule: a request is allowed only if `used + requested <= limit`; a request that would
cross the limit is rejected in full, never partially metered.

Concurrency: `test_concurrency_race_at_limit` races 2 threads, separate sessions,
`threading.Barrier(2)`, tenant seeded to `limit - 1`. Result: exactly one 200, one 429,
final usage exactly at limit — not over.

Lock mode: `SELECT ... FOR NO KEY UPDATE` (SQLAlchemy `with_for_update(key_share=True)`),
not `FOR UPDATE` as originally specified in DESIGN.md. Plain `FOR UPDATE` was tried and
reproduced a real `psycopg.errors.DeadlockDetected` — `claim()`'s insert into
`idempotency_keys` takes an automatic `FOR KEY SHARE` lock on the tenant row (FK
enforcement), which conflicts with `FOR UPDATE`. See DESIGN.md section 5 and BUILDLOG.md
for the full decision record.

---

## Phase 3c: HTTP layer (`POST /v1/generate`)
**Requirements: Metering API surface, validation at the boundary, 429/402/422/400/401 — Section 6**

```
$ docker compose exec app pytest -v        (run 1)
29 passed, 1 warning in 13.21s

$ docker compose exec app pytest -v        (run 2, repeated to rule out flakiness)
29 passed, 1 warning in 11.76s

$ docker compose exec db psql -U billing -d billing -c "SELECT count(*) FROM tenants;"
 count
-------
     2
```

Full suite, both runs, container (`platform linux`). Tenant count returned to exactly 2
after both runs, confirming `tests/conftest.py`'s cleanup fixture works and leaves no
cross-test leakage.

`tests/test_api_generate.py` (10 tests, all over real HTTP via `TestClient`):
- test_normal_request_returns_200_with_expected_cost
- test_idempotency_same_key_returns_identical_body_and_no_new_usage_events
- test_same_key_different_body_returns_422
- test_same_body_different_key_ordering_returns_replay_not_422 (proves key order in
  client JSON doesn't cause a false 422 — hash is computed canonically)
- test_missing_or_invalid_idempotency_key_returns_400
- test_missing_malformed_or_unknown_api_key_returns_identical_401 (missing header,
  wrong auth scheme, empty bearer token, and unknown key all return the identical
  401 body — a caller cannot distinguish "bad format" from "unknown key")
- test_negative_or_oversized_token_count_returns_422 (never 500)
- test_boundary_walk_over_http (999→1000 succeeds; 1001st returns 429 + Retry-After;
  retrying the 1001st with the same idempotency key replays the identical 429)
- test_past_due_tenant_returns_402
- test_unhandled_exception_returns_500_with_request_id (confirms no internal stack
  trace or exception text leaks into the client response)

Implementation: `app/api/deps.py` (auth), `app/api/schemas.py` (request/response models,
`extra="forbid"`), `app/api/routes/generate.py` (route — delegates all quota/cost logic
to `MeterService.record()`, computes nothing itself), `app/api/errors.py` (validation,
HTTP, and catch-all exception handlers), `app/main.py` (wiring).

---

## Phase 4: `GET /v1/usage` rollup

Requirements covered:
- authenticated GET /v1/usage
- current UTC calendar-month usage rollup
- API call usage and plan limits
- token usage by category and total
- cost recomputation using the existing pricing functions
- Decimal/string cost_usd display
- previous-month usage excluded
- Free/Pro plan limits isolated per tenant
- same 401 behavior as /v1/generate

Verification:

```text
$ docker compose exec app pytest tests/test_api_usage.py -v
8 passed
- test_empty_usage_returns_zeros
- test_usage_after_generate_matches_exactly
- test_cost_equals_stored_event_sum
- test_plan_isolation_free_and_pro
- test_previous_month_events_excluded
- test_missing_auth_returns_401
- test_invalid_auth_returns_401
- test_recomputed_cost_equals_stored_event_costs

$ docker compose exec app pytest -v
37 passed, 1 warning in 6.53s

$ docker compose exec app pytest -v
37 passed, 1 warning in 6.74s
```

Phase 4 API tests: 8/8 passed.

Notable tests:
- `test_previous_month_events_excluded`: explicitly inserts a `UsageEvent` with `occurred_at`
  set to the 15th of the previous UTC calendar month and asserts it does not appear in the rollup.
- `test_cost_equals_stored_event_sum`: cross-checks the API-returned `cost_usd` against the
  sum of `cost_usd` stored directly on the `usage_events` rows.
- `test_recomputed_cost_equals_stored_event_costs`: verifies that the service's recomputed
  cost (via the existing pricing functions) matches the costs already stored in `usage_events`.
- `test_plan_isolation_free_and_pro`: Free and Pro tenants each see their own plan limits,
  not each other's.
- `test_missing_auth_returns_401` / `test_invalid_auth_returns_401`: missing and invalid
  authentication returns the same 401 behavior as `/v1/generate`.

The full Docker suite passed twice with zero flakiness (6.53 s, 6.74 s).

## Phase 5: Stripe checkout and webhooks
_(pending)_

---

## Phase 5B: Stripe Webhooks

Requirements covered:
- POST /webhooks/stripe
- raw request body read before Stripe signature verification
- invalid signatures return 400 with zero database writes
- Stripe webhook signature verification using STRIPE_WEBHOOK_SECRET
- database-level webhook event deduplication using ON CONFLICT
- replayed events return 200 without repeating business logic
- checkout.session.completed creates/updates the subscription and promotes the tenant to Pro
- customer.subscription.updated synchronizes subscription state
- out-of-order subscription.updated events do not overwrite newer state
- customer.subscription.deleted cancels the subscription and reverts the tenant to Free
- unknown event types are recorded and return 200 without changing subscription state
- webhook event processing and subscription updates are atomic
- processed_at is committed only after successful processing

Verification:

```text
$ docker compose exec app pytest tests/test_webhooks.py -v
8 passed
Full Docker suite:
$ docker compose exec app pytest -v
49 passed, 1 warning in 8.48s

Previous full-suite run:
49 passed, 1 warning in 8.74s 
```
