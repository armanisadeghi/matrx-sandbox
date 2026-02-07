# Claude Code Agent Tasks

## Completed

| # | Task | Notes |
|---|------|-------|
| 1 | CRITICAL Code Review Fixes | Docker leak, input validation, race conditions, IAM scoping, shell hardening |
| 2 | HIGH Code Review Fixes | Retry logic, typed params, logging, memory growth |
| 3 | Unit Tests (20 tests) | sandbox_manager (10), storage (5), routes (5) |
| 4 | CI/CD Pipeline | `.github/workflows/ci.yml` + `deploy.yml` — all passing |
| 5 | docker-compose.yml Fixes | LocalStack endpoint, read-only Docker socket |

---

## Remaining

| # | Task | Priority |
|---|------|----------|
| 9 | API Key Authentication | **HIGH** |
| 6 | Structured Logging | MEDIUM |
| 7 | Postgres-Backed Sandbox Registry | MEDIUM |
| 8 | Integration Test Scaffolding | LOW |

---

### 9. API Key Authentication (HIGH — do this first)

**Why:** The orchestrator currently has **zero authentication**. Any endpoint can be called by anyone. We need API key auth so we can open port 8000 to the internet (multiple servers — Vercel, VPS, etc. — need to call it). SSH stays IP-locked; the API gets key-protected.

**What to build:**

1. **Middleware** — Create `orchestrator/orchestrator/middleware/auth.py`:
   - FastAPI middleware or dependency that checks for `X-API-Key` header (or `Authorization: Bearer <key>`)
   - Compare against `MATRX_API_KEY` env var using constant-time comparison (`hmac.compare_digest`)
   - Return 401 if missing, 403 if invalid
   - Skip auth for `GET /health` (so load balancers and monitoring can hit it unauthenticated)

2. **Config** — Add to `orchestrator/orchestrator/config.py`:
   - `api_key: str = ""` (env var: `MATRX_API_KEY`)
   - `api_key_header: str = "X-API-Key"` (env var: `MATRX_API_KEY_HEADER`)
   - If `api_key` is empty string at startup, log a **WARNING** that the API is running unauthenticated (don't crash — allows local dev without a key)

3. **Wire it up** — In `orchestrator/orchestrator/main.py`:
   - Import and add the auth middleware/dependency
   - Ensure it applies to all routes under `/sandboxes` and `/` but NOT `/health`

4. **Tests** — Add to `orchestrator/tests/test_routes.py`:
   - Test: request without key → 401
   - Test: request with wrong key → 403
   - Test: request with correct key → 200
   - Test: `/health` without key → 200 (exempt)

5. **Env files** — Add `MATRX_API_KEY=` to `.env.example` with a comment explaining it

**Do NOT:**
- Implement user-level auth or JWT — this is service-to-service auth only
- Add rate limiting (that's a separate concern)
- Change any existing endpoint signatures or response shapes

---

### 6. Structured Logging (MEDIUM)

Add structured JSON logging throughout the orchestrator:

- Use Python's `logging` module with JSON formatting (e.g. `python-json-logger`)
- Add FastAPI request/response middleware that logs method, path, status, duration
- Include `sandbox_id` and `user_id` as context in all sandbox lifecycle log entries
- Log all S3 operations with timing
- Configure via `MATRX_LOG_LEVEL` (default: INFO) and `MATRX_LOG_FORMAT` (default: json, option: text)
- Update `orchestrator/orchestrator/config.py` with the new settings
- Add `python-json-logger` to `requirements.txt`

---

### 7. Postgres-Backed Sandbox Registry (MEDIUM)

The `SandboxManager` uses an in-memory dict (`_sandboxes` in `sandbox_manager.py` line 23) — all state is lost on restart. We already have a Supabase project with Postgres, so use that instead of SQLite.

**What to build:**

1. **Abstract store** — Create `orchestrator/orchestrator/store.py`:
   - Abstract base class `SandboxStore` with methods: `save(sandbox)`, `get(sandbox_id)`, `list(user_id=None)`, `delete(sandbox_id)`, `update_status(sandbox_id, status)`
   - `InMemorySandboxStore` — wraps current dict behavior (default, for local dev)
   - `PostgresSandboxStore` — uses `asyncpg` to connect to Supabase/Postgres

2. **Config** — Add to `orchestrator/orchestrator/config.py`:
   - `sandbox_store: str = "memory"` (env var: `MATRX_SANDBOX_STORE`, options: `memory` or `postgres`)
   - `database_url: str = ""` (env var: `MATRX_DATABASE_URL`, standard Postgres connection string)

3. **Schema** — Create a migration SQL file at `orchestrator/migrations/001_create_sandboxes.sql`:
   ```sql
   CREATE TABLE IF NOT EXISTS sandboxes (
       sandbox_id TEXT PRIMARY KEY,
       user_id TEXT NOT NULL,
       status TEXT NOT NULL DEFAULT 'creating',
       container_id TEXT,
       created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
       updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
       hot_path TEXT,
       cold_path TEXT,
       config JSONB DEFAULT '{}'
   );
   CREATE INDEX idx_sandboxes_user_id ON sandboxes(user_id);
   CREATE INDEX idx_sandboxes_status ON sandboxes(status);
   ```

4. **Reconciliation** — On startup with Postgres store, reconcile DB state with actual Docker containers:
   - Any sandbox in DB marked READY/RUNNING but no matching container → mark STOPPED
   - Any running container not in DB → log warning (don't auto-destroy)

5. **Wire it up** — In `sandbox_manager.py`, replace direct `_sandboxes` dict access with calls to the store interface

6. **Dependencies** — Add `asyncpg` to `requirements.txt`

**Do NOT:**
- Run migrations automatically — just provide the SQL file
- Add an ORM (SQLAlchemy, Tortoise, etc.) — raw asyncpg is fine for this

---

### 8. Integration Test Scaffolding (LOW)

Create `orchestrator/tests/test_integration.py`:

- Full sandbox lifecycle test against docker-compose + LocalStack
- Tests: create sandbox, exec command, verify S3 storage, destroy, verify cleanup
- Pytest fixture that starts/stops docker-compose
- Mark all tests with `@pytest.mark.integration` so they're skipped in CI without Docker
- Add `make test-integration` target or document how to run separately
- If API key auth (Task 9) is done, include the key in test requests
