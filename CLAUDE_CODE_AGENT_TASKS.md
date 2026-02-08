# Claude Code Agent Tasks

## Completed

| # | Task | Notes |
|---|------|-------|
| 1 | CRITICAL code review fixes | Docker leak, input validation, race conditions, IAM scoping, shell hardening |
| 2 | HIGH code review fixes | Retry logic, typed params, logging, memory growth |
| 3 | Unit tests (20 tests) | sandbox_manager, storage, routes |
| 4 | CI/CD pipeline | `ci.yml` (auto) + `deploy.yml` (manual) |
| 5 | docker-compose.yml fixes | LocalStack endpoint, read-only Docker socket |
| 6 | Structured logging | JSON logging, request/response middleware, deployed |
| 7 | Postgres sandbox store | `sandbox_instances` in Supabase, store abstraction, reconciliation, expiry, heartbeat, stop reasons |
| 8 | Integration test scaffolding | `test_integration.py`, `conftest.py`, `--run-integration` flag, `make test-integration` |
| 9 | API key authentication | Middleware, config, tests, deployed and verified |

---

## Remaining

| # | Task | Priority | Notes |
|---|------|----------|-------|
| 10 | Complete deploy workflow | HIGH | `deploy.yml` builds/pushes images but doesn't deploy to EC2 |
| 11 | ai-matrx-admin env vars | HIGH | Set `MATRX_ORCHESTRATOR_URL` + `MATRX_ORCHESTRATOR_API_KEY` |
| 12 | Sandbox dashboard page | MEDIUM | `/dashboard/sandbox` — list, create, manage instances |
| 13 | Sandbox detail page | MEDIUM | `/dashboard/sandbox/[id]` — terminal, status, controls |
| 14 | Dashboard sandbox widget | LOW | Summary widget on main dashboard |
| 15 | Regenerate Supabase types | LOW | Run Supabase type gen to pick up `sandbox_instances` |

---

### 10. Complete Deploy Workflow (HIGH)

The `deploy.yml` currently builds and pushes Docker images to ECR but has a `TODO` for the actual EC2 deployment step. The orchestrator runs as a systemd service on EC2, not in a container, so the deploy step needs to:

1. SSH into EC2 and pull the latest orchestrator code (or SCP the files)
2. Install any new Python dependencies
3. Restart the systemd service

**Requires:** EC2 SSH key stored as GitHub Secret (`EC2_SSH_KEY`).

**In `.github/workflows/deploy.yml`, add after the image push steps:**

```yaml
- name: Deploy orchestrator to EC2
  env:
    EC2_HOST: 54.144.86.132
    EC2_USER: ec2-user
  run: |
    echo "${{ secrets.EC2_SSH_KEY }}" > /tmp/ec2_key.pem
    chmod 600 /tmp/ec2_key.pem
    scp -o StrictHostKeyChecking=no -i /tmp/ec2_key.pem -r orchestrator/orchestrator/ $EC2_USER@$EC2_HOST:/home/ec2-user/orchestrator/orchestrator/
    ssh -o StrictHostKeyChecking=no -i /tmp/ec2_key.pem $EC2_USER@$EC2_HOST "pip3.11 install -e /home/ec2-user/orchestrator && sudo systemctl restart matrx-orchestrator"
    rm /tmp/ec2_key.pem
```

**Also requires:** Adding the EC2 SSH private key as a GitHub Secret named `EC2_SSH_KEY`.

---

### 11. ai-matrx-admin Environment Variables (HIGH)

The cloud agent already created API routes and React hooks in `ai-matrx-admin`:
- `/api/sandbox/route.ts` — List + Create
- `/api/sandbox/[id]/route.ts` — Detail + Stop + Delete
- `/api/sandbox/[id]/exec/route.ts` — Execute commands
- `hooks/sandbox/use-sandbox.ts` — React hook
- `types/sandbox.ts` — TypeScript types

These require two environment variables to be set in the `ai-matrx-admin` project:

```
MATRX_ORCHESTRATOR_URL=http://54.144.86.132:8000
MATRX_ORCHESTRATOR_API_KEY=<the API key from .env>
```

Set these in the Vercel project settings (or `.env.local` for local dev).

---

### 12. Sandbox Dashboard Page (MEDIUM)

Create `/dashboard/sandbox` page in `ai-matrx-admin`:
- List all user's sandbox instances (from Supabase via RLS)
- Create new sandbox button (calls orchestrator via API route)
- Show status, created time, actions (stop, destroy)
- Auto-refresh or realtime updates via Supabase subscription

---

### 13. Sandbox Detail Page with Terminal (MEDIUM)

Create `/dashboard/sandbox/[id]` page in `ai-matrx-admin`:
- Show sandbox status, container info, timestamps
- Embedded terminal that sends commands via `/api/sandbox/[id]/exec`
- Stop/Destroy actions
- File browser (future — reads from hot storage)

---

### 14. Dashboard Sandbox Widget (LOW)

Add a summary card to the main dashboard showing:
- Number of active sandboxes
- Quick create button
- Link to full sandbox dashboard

---

### 15. Regenerate Supabase TypeScript Types (LOW)

Run Supabase type generation to include the `sandbox_instances` table in the TypeScript types used by `ai-matrx-admin`:

```bash
npx supabase gen types typescript --project-id txzxabzwovsujtloxrus > types/supabase.ts
```
