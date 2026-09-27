# EVIDENCE.md

One pasted proof per Requirements checkbox (Section 6 of the brief). Filled in as each phase completes.

---

## Phase 0 — App skeleton / healthz

```
$ curl -i localhost:8000/healthz
HTTP/1.1 200 OK
{"status":"ok"}
```
Confirmed this is a real DB round trip (SELECT 1), not a hardcoded string — verified by stopping the db container and confirming /healthz stops returning 200.

---

## Phase 1 — Data model, migrations, seed

```
$ docker compose exec app alembic upgrade head
$ docker compose exec app python scripts/seed.py
tenant name=seed-free plan=free
tenant name=seed-pro  plan=pro
```

Schema verified against 0001_initial.sql via `\d usage_events`, `\d idempotency_keys`, `\d subscriptions`:
- ck_category_matches_type, ck_usage_events_cost, ck_usage_events_quantity, ck_usage_events_unit_price present
- uq_idem_tenant_key UNIQUE CONSTRAINT on (tenant_id, key) present
- idx_sub_one_active_per_tenant partial UNIQUE index (WHERE status = ANY(...)) present

Idempotency of seed script confirmed: re-ran scripts/seed.py multiple times,
`SELECT count(*) FROM tenants` stayed at 2. Fixed via ON CONFLICT (api_key_hash) DO NOTHING
with a deterministic seed-derived key, replacing an earlier SELECT-then-INSERT approach
that allowed a 3rd row to appear (traced to a test writing into the same DB — separate
test-isolation issue, deferred to Phase 2/3).

---

## Phase 2 — Cost calculation (Requirement: Cost calculation, Section 6)

Phase 2 — Cost calculator (Requirement: Cost calculation, Section 6)

docker compose exec app pytest tests/test_cost.py -v
5 passed in 0.70s
- test_cached_input_rate_is_ten_percent_of_normal_input_rate
- test_reasoning_tokens_priced_identically_to_output_tokens
- test_regression_naive_sum_times_input_rate_differs_from_total
- test_worked_example_literal_micro_cents
- test_price_api_call

Worked example: input=1000 (300,000 µ¢), cached_input=500 (15,000 µ¢),
output=200 + reasoning=100 folded to output bucket (450,000 µ¢)
Total = 765,000 µ¢

Implementation: app/services/cost.py (price_tokens, price_api_call)
Constants: app/config/pricing.py (Pricing dataclass, PRICING)
## Phase 3 — Idempotent metering
## Phase 3a — Idempotency claim mechanism

docker compose exec app pytest tests/test_idempotency.py -v
5 passed in 5.11s (platform linux, container)
- test_first_claim_fresh_key
- test_claim_replay_after_complete
- test_claim_hash_mismatch
- test_claim_in_progress
- test_concurrency_claims (12 real threads, separate DB sessions,
  threading.Barrier forces true simultaneous claim() calls;
  exactly 1 OWNED, 11 IN_PROGRESS)

Mechanism: Postgres INSERT ... ON CONFLICT (tenant_id, key) DO NOTHING
RETURNING id — DB-level uniqueness, not read-then-write.

## Phase 4 — Quota enforcement

_(pending)_

## Phase 5 — Stripe integration

_(pending)_