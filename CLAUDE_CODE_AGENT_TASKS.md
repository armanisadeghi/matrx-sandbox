# Claude Code Agent Tasks

Tasks for the Claude Code agent. Tracks coding work across sessions.

---

## Status

| # | Task | Status | Priority |
|---|------|--------|----------|
| 1 | Apply Code Review Fixes (CRITICAL) | DONE | CRITICAL |
| 2 | Apply Code Review Fixes (HIGH) | DONE | HIGH |
| 3 | Expand Unit Test Coverage | DONE | HIGH |
| 4 | Add CI/CD Pipeline (GitHub Actions) | DONE | MEDIUM |
| 5 | Fix docker-compose.yml Issues | DONE | MEDIUM |
| 6 | Add Structured Logging | TODO | MEDIUM |
| 7 | Add Database-Backed Sandbox Registry | TODO | LOW |
| 8 | Add Integration Test Scaffolding | TODO | LOW |

---

## Completed Work (2026-02-07)

### Code Review Fixes Applied

**CRITICAL issues fixed:**
- C1/H5: Race condition — container status validated before exec (`container.reload()`)
- C2: Input validation — command length limits, Pydantic validators on ExecRequest
- C3: SSH/API open to 0.0.0.0/0 — configurable `ssh_cidr_blocks`/`api_cidr_blocks` (default: empty)
- C4: ECR IAM policy Resource=* — split into scoped statements with `ecr_repo_arn` variable
- C5: Checksum verification — mount-s3 pinned to v1.12.0, TODOs for SHA256 on ripgrep/fd
- C6: Shell variable validation — all scripts validate S3_BUCKET and USER_ID at startup
- C7: Docker client leak — singleton pattern with `close_docker_client()` on app shutdown
- C8: S3 bucket validation — `validate_bucket()` called at startup via FastAPI lifespan
- C9: bootstrap-host.sh — added ERR trap for error reporting

**HIGH issues fixed:**
- H1: HTTP retry logic — `SandboxClient._request_with_retry()` with exponential backoff
- H2: Typed route params — `CompletionRequest`, `ErrorReport` replace untyped dicts
- H3: Docstrings on key functions
- H4: Security logging — module-level loggers, lifecycle events logged
- H6: Memory growth — `_sandboxes.pop()` on destroy
- H7: Pinned mount-s3 to 1.12.0
- H8: Docker socket — read-only mount, `no-new-privileges`
- M3: S3 config validation via `field_validator`
- M6: Logging import moved to module level
- M7: SandboxClient caching at init
- M8: user_id validation — regex `^[a-zA-Z0-9._-]{1,255}$`

### Docker-compose Fixes
- LocalStack: `AWS_ENDPOINT_URL_S3=http://localstack:4566` + `depends_on`
- Docker socket: read-only mount with `no-new-privileges`

### Tests (20 tests)
- `test_sandbox_manager.py`: 10 tests
- `test_storage.py`: 5 tests
- `test_routes.py`: 5 tests (ASGI TestClient)

### CI/CD Pipeline
- `.github/workflows/ci.yml`: Tests + Docker builds on push/PR
- `.github/workflows/deploy.yml`: Manual ECR push

---

## Remaining Tasks

### 6. Add Structured Logging (MEDIUM)

- JSON-formatted logging for production
- Request logging middleware for FastAPI
- sandbox_id/user_id context in all log entries
- Configure via MATRX_LOG_LEVEL and MATRX_LOG_FORMAT

### 7. Add Database-Backed Sandbox Registry (LOW)

- Abstract `SandboxStore` interface
- `InMemorySandboxStore` (current, default)
- `SqliteSandboxStore` (single-node production)
- Configurable via `MATRX_SANDBOX_STORE`

### 8. Add Integration Test Scaffolding (LOW)

- `orchestrator/tests/test_integration.py`
- Full lifecycle against docker-compose + LocalStack
- `@pytest.mark.integration` marker
