# Claude Code Agent Tasks

## Completed

| # | Task | Notes |
|---|------|-------|
| 1 | CRITICAL Code Review Fixes | Docker leak, input validation, race conditions, IAM scoping, shell hardening |
| 2 | HIGH Code Review Fixes | Retry logic, typed params, logging, memory growth, mount-s3 pinning |
| 3 | Unit Tests (20 tests) | sandbox_manager (10), storage (5), routes (5) |
| 4 | CI/CD Pipeline | `.github/workflows/ci.yml` + `deploy.yml` |
| 5 | docker-compose.yml Fixes | LocalStack endpoint, read-only Docker socket |

## Remaining

| # | Task | Priority |
|---|------|----------|
| 6 | Structured Logging | MEDIUM |
| 7 | Database-Backed Sandbox Registry | LOW |
| 8 | Integration Test Scaffolding | LOW |

### 6. Structured Logging

JSON-formatted logging, FastAPI request middleware, sandbox_id/user_id context
in all entries. Configure via `MATRX_LOG_LEVEL` and `MATRX_LOG_FORMAT`.

### 7. Database-Backed Sandbox Registry

Abstract `SandboxStore` interface with `InMemorySandboxStore` (default) and
`SqliteSandboxStore` (production). Configure via `MATRX_SANDBOX_STORE`.

### 8. Integration Test Scaffolding

`orchestrator/tests/test_integration.py` — full lifecycle against docker-compose
+ LocalStack. Mark with `@pytest.mark.integration`.
