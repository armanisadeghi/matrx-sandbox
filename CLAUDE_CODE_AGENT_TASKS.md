# Claude Code Agent Tasks

## Completed

| # | Task | Notes |
|---|------|-------|
| 1 | CRITICAL code review fixes | Docker leak, input validation, race conditions, IAM scoping, shell hardening |
| 2 | HIGH code review fixes | Retry logic, typed params, logging, memory growth |
| 3 | Unit tests (25 tests) | sandbox_manager, storage, routes — all passing |
| 4 | CI/CD pipeline | `ci.yml` (auto) + `deploy.yml` (push to main + manual) |
| 5 | docker-compose.yml | LocalStack default + production profile with real backends |
| 6 | Structured logging | JSON logging, request/response middleware, deployed |
| 7 | Postgres sandbox store | `sandbox_instances` in Supabase, store abstraction, reconciliation, expiry, heartbeat, stop reasons |
| 8 | Integration test scaffolding | `test_integration.py`, `conftest.py`, `--run-integration` flag, `make test-integration` |
| 9 | API key authentication | Middleware, config, tests, deployed and verified |
| 10 | Deploy workflow (SSM) | Builds/pushes ECR images, deploys via SSM (not SSH), health check loop |
| 11 | ai-matrx-admin env vars | `MATRX_ORCHESTRATOR_URL` + `MATRX_ORCHESTRATOR_API_KEY` set in `.env.local` and Vercel |
| 12 | Sandbox dashboard page | `/sandbox` — list, create, manage instances (ai-matrx-admin) |
| 13 | Sandbox detail page | `/sandbox/[id]` — terminal UI, status, controls (ai-matrx-admin) |
| 14 | Sandbox API routes | `route.ts` (list/create), `[id]/route.ts` (detail/stop/delete), `[id]/exec/route.ts` (execute) |
| 15 | React hooks + types | `use-sandbox.ts` hook, `types/sandbox.ts` TypeScript types |
| 16 | Multi-arch Dockerfile | ripgrep/fd via apt, AWS CLI multi-arch, mountpoint-s3 conditional (x86_64 only) |
| 17 | Remove fake simulation code | All `sbx-dev-*` simulation removed from ai-matrx-admin API routes |

---

## Remaining

| # | Task | Priority | Notes |
|---|------|----------|-------|
| 18 | Dashboard sandbox widget | LOW | Summary widget on main dashboard |
| 19 | Regenerate Supabase types | LOW | Run `npx supabase gen types` to pick up `sandbox_instances` |
| 20 | WebSocket/streaming exec | MEDIUM | Replace polling with streaming command output |
| 21 | File browser component | MEDIUM | Browse hot storage files from the sandbox detail page |
| 22 | Sandbox TTL extension UI | LOW | Let users extend sandbox lifetime from the UI |
