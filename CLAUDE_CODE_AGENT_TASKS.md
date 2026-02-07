# Claude Code Agent Tasks (Remote/Cloud)

Tasks for the Claude Code agent that has full codebase access and can push
to git, but does NOT have access to the local environment, browser, or AWS
Console.

---

## Status

| # | Task | Status | Priority |
|---|------|--------|----------|
| 1 | Add Orchestrator Dockerfile | TODO | HIGH |
| 2 | Add Unit Test Coverage | TODO | HIGH |
| 3 | Add Integration Test Scaffolding | TODO | MEDIUM |
| 4 | Add CI/CD Pipeline (GitHub Actions) | TODO | MEDIUM |
| 5 | Add Logging & Monitoring | TODO | LOW |
| 6 | Add Database-Backed Sandbox Registry | TODO | LOW |
| 7 | Apply Code Review Fixes | **READY** | CRITICAL — see `docs/CODE_REVIEW.md` |

---

## Infrastructure Notes (from Arman's deployment 2026-02-07)

- **EC2 Instance**: `i-084f757c1e47d4efb` at `44.204.37.36` (t3.xlarge, us-east-1)
- **Sandbox image** was built directly on EC2 (not pushed to ECR yet). CI/CD should handle ECR push.
- **EC2 IAM role** (`matrx-sandbox-host-dev`) has ECR pull-only permissions. CI/CD pipeline needs separate push credentials via GitHub Secrets.
- **Bug fix applied**: `sandbox-image/scripts/cold-mount.sh` — added `mkdir -p /tmp/s3cache` before `mount-s3` call (cache dir wasn't being created).
- **Docker daemon fix**: `bootstrap-host.sh` ulimits entry has been removed from the repo (conflicts with Amazon Linux 2023 systemd). Already fixed in code.
- **LocalStack issue**: `docker-compose.yml` doesn't configure the orchestrator to connect to LocalStack. The orchestrator's boto3 client tries real AWS S3 instead of `http://localstack:4566`. Fix: add `AWS_ENDPOINT_URL_S3=http://localstack:4566` to the orchestrator environment in docker-compose.yml, and add `depends_on: [localstack]`.

---

## 1. Add Orchestrator Dockerfile

The `docker-compose.yml` references building from `./orchestrator/Dockerfile`,
but no Dockerfile exists in the orchestrator directory. Create one.

**Prompt for agent**:

> The `docker-compose.yml` in the repo root references `build: ./orchestrator`
> but there is no `Dockerfile` in the `orchestrator/` directory. Create
> `orchestrator/Dockerfile` that:
> - Uses `python:3.11-slim` as base
> - Installs the project dependencies from `pyproject.toml`
> - Exposes port 8000
> - Runs `uvicorn orchestrator.main:app --host 0.0.0.0 --port 8000`
> - Mounts the Docker socket (handled by compose, but document in comments)
>
> Also verify that `docker-compose.yml` is consistent with this Dockerfile.

---

## 2. Add Unit Test Coverage

The existing tests in `orchestrator/tests/` are a good start but likely need
expansion. The sandbox_manager and storage modules have complex logic.

**Prompt for agent**:

> Review the existing tests in `orchestrator/tests/test_sandbox_manager.py`
> and `orchestrator/tests/test_storage.py`. Expand test coverage to include:
> - Edge cases (container timeout, S3 errors, invalid sandbox IDs)
> - All sandbox lifecycle states (CREATING → READY → RUNNING → STOPPED)
> - Error handling paths in sandbox_manager.py
> - Storage cleanup and stats calculation
> - Route handler tests using FastAPI TestClient
>
> Use pytest with unittest.mock for Docker and boto3 mocking. Ensure all
> tests can run without real AWS or Docker connections.

---

## 3. Add Integration Test Scaffolding

Set up integration tests that work with the docker-compose local environment.

**Prompt for agent**:

> Create `orchestrator/tests/test_integration.py` with integration tests
> that work against the docker-compose environment (orchestrator + LocalStack).
> Tests should:
> - Create a sandbox via the API
> - Execute a command in the sandbox
> - Verify S3 storage was created (via LocalStack)
> - Destroy the sandbox gracefully
> - Verify cleanup
>
> Include a pytest fixture that starts docker-compose and tears it down.
> Mark these tests with `@pytest.mark.integration` so they can be skipped
> in CI when Docker isn't available.

---

## 4. Add CI/CD Pipeline (GitHub Actions)

**Prompt for agent**:

> Create `.github/workflows/ci.yml` with a GitHub Actions pipeline that:
> - Triggers on push to main and PRs
> - Runs linting (ruff or flake8)
> - Runs unit tests with pytest
> - Builds the sandbox Docker image (no push)
> - Builds the orchestrator Docker image (no push)
> - Reports test coverage
>
> Also create `.github/workflows/deploy.yml` (manual trigger only) that:
> - Builds and pushes sandbox image to ECR
> - Builds and pushes orchestrator image to ECR
> - (Document but don't implement: SSH to EC2 and pull new images)
>
> Use GitHub Secrets for AWS credentials. Add a note about required secrets.

---

## 5. Add Logging & Monitoring

**Prompt for agent**:

> Add structured logging throughout the orchestrator:
> - Use Python's `logging` module with JSON formatting
> - Log all sandbox lifecycle events (create, ready, exec, destroy)
> - Log all S3 operations with timing
> - Add request logging middleware to FastAPI
> - Include correlation IDs (sandbox_id) in all log entries
>
> Update config.py to support log level configuration via MATRX_LOG_LEVEL
> environment variable.

---

## 6. Add Database-Backed Sandbox Registry

The current sandbox registry is in-memory (dict). For production, this needs
persistence.

**Prompt for agent**:

> The SandboxManager currently uses an in-memory dict (`self._sandboxes`)
> to track sandboxes. This means all state is lost on orchestrator restart.
>
> Refactor to support pluggable storage backends:
> - Create an abstract `SandboxStore` interface
> - Implement `InMemorySandboxStore` (current behavior)
> - Implement `SqliteSandboxStore` (for single-node production)
> - Make the store configurable via MATRX_SANDBOX_STORE env var
>
> Keep the in-memory store as default for development.

---

## 7. Apply Code Review Fixes — READY

Code review is complete. Full report at `docs/CODE_REVIEW.md`.
45 issues found (9 CRITICAL, 10 HIGH, 11 MEDIUM, 10 LOW).

**Prompt for agent**:

> Read `docs/CODE_REVIEW.md` for the full code review report. Fix all CRITICAL
> and HIGH issues. The most important fixes are:
>
> **CRITICAL (fix first):**
> 1. `sandbox_manager.py`: Fix Docker client resource leak — clients are created
>    but never closed. Use a context manager or singleton pattern.
> 2. `sandbox_manager.py`: Add input validation on shell commands in exec_in_sandbox.
>    Validate command length, add timeout to exec_run call.
> 3. `sandbox_manager.py`: Fix race condition — validate container is running
>    before exec. Check sandbox.status and container.status.
> 4. `storage.py`: Validate S3 bucket exists at startup, not at first use.
>    Fail fast if MATRX_S3_BUCKET is empty or bucket doesn't exist.
> 5. `ec2.tf`: Add variables for ssh_cidr_blocks and api_cidr_blocks to replace
>    the 0.0.0.0/0 ingress rules. Default to empty (require explicit config).
> 6. `iam.tf`: Scope ECR policy Resource from "*" to specific repo ARN.
>    Keep GetAuthorizationToken on "*" (required by AWS).
> 7. Shell scripts: Add variable validation at top of entrypoint.sh, hot-sync.sh,
>    cold-mount.sh — exit immediately if required vars are empty.
> 8. Dockerfile: Add SHA256 checksum verification for all curl downloads.
> 9. `bootstrap-host.sh`: Add error trap and checksum verification for
>    Docker Compose download.
>
> **HIGH (fix second):**
> 1. `sandbox_manager.py`: Clean up _sandboxes dict after destroy to prevent
>    unbounded memory growth.
> 2. `sandboxes.py`: Replace untyped `dict` params with Pydantic models for
>    /complete and /error endpoints.
> 3. `sandboxes.py`: Move `import logging` to module level. Add structured
>    logging with sandbox_id, user_id context to exec and error endpoints.
> 4. `client.py`: Add retry logic and proper exception handling for httpx calls.
>    Cache SandboxInfo instead of re-reading env vars on every call.
> 5. `models.py`: Add field_validator for user_id — alphanumeric, dots, dashes,
>    underscores only, 1-255 chars.
> 6. `variables.tf`: Add validation blocks for s3_bucket_name format and
>    ec2_instance_type format.
> 7. `cold-mount.sh`: Add 30-second timeout to mount-s3 command.
> 8. `hot-sync.sh`: Clean up partial files on sync failure before retry.
> 9. `docker-compose.yml`: Document Docker socket security implications.
> 10. `provider.tf`: Uncomment and configure S3 remote state backend.
>
> After fixing, run any existing tests to make sure nothing broke.
