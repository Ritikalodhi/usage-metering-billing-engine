# BUILDLOG.md

AI-usage log. Tools: Claude (design, plan, prompt writing, review) and Cursor/Antigravity (code generation). Entries record where AI helped, where it was wrong, and what I changed.

---

## Phase 0: Skeleton
- AI generated the FastAPI layout and `/healthz`.
- Checked myself that `/healthz` does a real `SELECT 1` by stopping the db container and confirming it stops returning 200.

## Phase 1: Data layer
- AI translated `0001_initial.sql` into SQLAlchemy models. I diffed the result with `\d` in psql; the partial unique index, composite unique constraint and CHECK constraints all landed.
- **AI was wrong:** the first `seed.py` used SELECT-then-INSERT on `name`, which has no unique constraint. `SELECT count(*) FROM tenants` returned 3 instead of 2. Root cause of the extra row was `tests/test_data_layer.py` writing into the live app database. Fixed the seed with `INSERT ... ON CONFLICT (api_key_hash) DO NOTHING`.
- Open issue: tests still share the app database. Needs a separate test DB or transactional fixture before the suite grows further.
- Seed keys are derived from a fixed phrase on purpose. Seed data only; never do this for real tenant keys.

## Phase 2: Cost calculator
- **AI was wrong:** Cursor reported `tests/test_cost.py` was created and passing. `dir tests` showed the file did not exist. Re-prompted and it was created on the second attempt.
- **AI was wrong:** the calculator lived in `app/config/pricing.py` and `app/services/cost.py` was a 3-line re-export. `price_api_call()` was missing entirely. Caught by reading `git diff`. Re-prompted with an explicit boundary: config = constants only, services = logic.
- Lesson: verify "done" claims against the filesystem and against the container, not against the AI's summary.

## Phase 3a: Idempotency claim
- AI wrote `claim()` and `complete()` using `INSERT ... ON CONFLICT (tenant_id, key) DO NOTHING RETURNING id`.
- I reviewed the code and the concurrency test directly: 12 real threads, separate sessions, `threading.Barrier`, exact-count assertions.
- Minor note: the final fallback `return Claim(status="IN_PROGRESS")` in `claim()` is unreachable unless a row is deleted mid-flight. If it ever fires, investigate rather than ignore.

## Phase 3b: MeterService and lock mode
- **AI deviated silently:** DESIGN.md specified `SELECT ... FOR UPDATE`; Cursor used `FOR NO KEY UPDATE` (`with_for_update(key_share=True)`) without flagging the change. Caught by comparing the code to DESIGN.md.
- I asked for a revert to `FOR UPDATE` and a re-test instead of accepting the change. The revert reproduced a real PostgreSQL `DeadlockDetected` in `test_concurrency_race_at_limit`: `claim()`'s insert into `idempotency_keys` takes an automatic `FOR KEY SHARE` lock on the tenant row (FK enforcement), which conflicts with `FOR UPDATE`. Two concurrent requests deadlock by design while waiting to upgrade the tenant lock.
- Considered locking the tenant row before `claim()`. Rejected: replayed requests would then take the exclusive lock before short-circuiting.
- Decision: keep `FOR NO KEY UPDATE`. It is compatible with the FK key-share lock, still serializes concurrent metering, and the concurrency test proves it. Updated DESIGN.md section 5 with the reasoning. (I first told myself a concurrent plan change would not be blocked by FOR NO KEY UPDATE; that was wrong, since a plain UPDATE of plan_id takes the same lock. Corrected in DESIGN.md.)
- Cursor's test summaries repeatedly came from host Python (win32) instead of the container. I re-run everything with `docker compose exec app pytest` before committing.
- Test-isolation issue also surfaced during Docker verification: `test_data_layer.py` used a fixed `sha256(b"test_key")` `api_key_hash` and tests shared the application database. The first run passed but the next run failed with a unique constraint violation. Fixed by using a random hash and adding `tests/conftest.py` cleanup for test-created tenants. Docker suite then passed twice consecutively with 19 passed each time, and the billing database returned to exactly 2 seed tenants.

## Phase 3c: HTTP layer (POST /v1/generate)
- AI implemented route handlers, auth dependency via Bearer token hash lookup, Pydantic schema validation (`extra="forbid"`), and global error handlers.
- Error response sanitization: caught and ensured 500 errors return opaque `request_id` without leaking stack traces or internal configuration.
- Verified idempotency replay over HTTP: duplicate request with same payload returns cached body and 200 without re-metering; payload mismatch returns 422.

## Phase 4: Usage rollup (GET /v1/usage)
- AI implemented `GET /v1/usage` aggregating current calendar-month usage directly from `usage_events`.
- Strict boundary check verified: events from previous UTC calendar months are excluded from rollup calculations.
- Plan isolation verified: Free and Pro tenants only see their respective limits and metrics.

## Phase 5A: Stripe checkout (POST /v1/billing/checkout)
- AI added Stripe checkout session generation with customer creation/lookup and metadata linking.
- All Stripe API interactions mocked during testing to avoid external network calls.

## Phase 5B: Stripe webhooks (POST /webhooks/stripe)
- AI added `POST /webhooks/stripe` with signature verification, raw body reading, deduplication, and lifecycle handlers (`checkout.session.completed`, `customer.subscription.updated`, `customer.subscription.deleted`).
- **AI was wrong / Driver Syntax Error**: Initial raw SQL used `:payload::jsonb` for the webhook payload insert. The `psycopg` driver parsed `:payload:` as a named parameter marker conflicting with the PostgreSQL `::jsonb` type cast. Fixed by switching to standard SQL `CAST(:payload AS jsonb)`.
- Verified security constraint: raw body is read before signature verification; signature validation failure returns 400 immediately with zero database interactions.
- Verified atomic deduplication: `INSERT INTO webhook_events ... ON CONFLICT (stripe_event_id) DO NOTHING RETURNING stripe_event_id`. Replays return HTTP 200 with `{"status": "replay"}`.
- Verified out-of-order event protection: incoming events with timestamps older than `last_event_at` are safely ignored for subscription updates while still logging the webhook event.
- All 49 tests pass cleanly across two consecutive runs in Docker.