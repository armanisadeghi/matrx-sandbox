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
| 6 | Structured Logging | MEDIUM |
| 7 | Database-Backed Sandbox Registry | LOW |
| 8 | Integration Test Scaffolding | LOW |

### 6. Structured Logging

Add structured JSON logging throughout the orchestrator:

- Use Python's `logging` module with JSON formatting
- Add FastAPI request/response middleware that logs method, path, status, duration
- Include `sandbox_id` and `user_id` as context in all sandbox lifecycle log entries
- Log all S3 operations with timing
- Configure via `MATRX_LOG_LEVEL` (default: INFO) and `MATRX_LOG_FORMAT` (default: json, option: text)
- Update `orchestrator/orchestrator/config.py` with the new settings

### 7. Database-Backed Sandbox Registry

The `SandboxManager` uses an in-memory dict (`self._sandboxes`) — all state is lost on restart.

- Create abstract `SandboxStore` interface in `orchestrator/orchestrator/store.py`
- Implement `InMemorySandboxStore` (current behavior, default)
- Implement `SqliteSandboxStore` for single-node production persistence
- Configure via `MATRX_SANDBOX_STORE` env var (`memory` or `sqlite`)
- On startup with sqlite, reconcile store state with actual Docker containers

### 8. Integration Test Scaffolding

Create `orchestrator/tests/test_integration.py`:

- Full sandbox lifecycle test against docker-compose + LocalStack
- Tests: create sandbox, exec command, verify S3 storage, destroy, verify cleanup
- Pytest fixture that starts/stops docker-compose
- Mark all tests with `@pytest.mark.integration` so they're skipped in CI without Docker
- Add `make test-integration` target or document how to run separately
