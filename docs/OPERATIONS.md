# Matrx Sandbox — Operations

Operational runbook for the two sandbox tiers. For architecture (storage tiers, lifecycle, deploy pipeline) see [ARCHITECTURE.md](ARCHITECTURE.md). For the HTTP API see [SANDBOX_CLIENT_GUIDE.md](../SANDBOX_CLIENT_GUIDE.md).

---

## The two tiers

| | EC2 tier | Hosted tier |
|---|---|---|
| Orchestrator URL | `http://54.144.86.132:8000` | `https://orchestrator.dev.codematrx.com` |
| Code | `/srv/projects/matrx-sandbox/orchestrator/` (same repo, same code) | same |
| Where it lives | EC2 instance, single host | This server (`/srv/apps/sandbox-orchestrator/`) |
| Sandbox storage | S3 hot-sync + FUSE cold | Docker named volumes per sandbox |
| Sandbox image | `matrx-sandbox:latest` (production build, no ttyd) | `matrx-sandbox:local` (adds ttyd for browser shells) |
| Metadata store | Supabase Postgres (`sandbox_instances` table, RLS per user) | In-memory today (state lost on restart). Postgres-backed is a follow-up. |
| Default TTL | 7200 s (2 h), auto-shutdown | 7200 s (extendable; sessions can stay alive indefinitely if pinged) |
| Per-sandbox limits | 2 CPU / 4 GB / 20 GB | Configurable via `resources` field on create; default same as EC2 |
| Deploy mechanism | Push to `main` → GHA → ECR build → SSM → restart on EC2 | Local `docker compose up -d` after `docker build` |
| API key | `MATRX_API_KEY` on EC2 | `MATRX_API_KEY` in `/srv/apps/sandbox-orchestrator/.env` (also recorded in `/srv/.credentials`) |

Both orchestrators advertise their tier via `GET /` and `GET /api-surface`. A `POST /sandboxes` request whose `tier` doesn't match the orchestrator's `MATRX_HOST_TIER` is rejected with HTTP 400 — there is no cross-tier proxying. Frontends route by reading the sandbox row's `tier` column.

---

## Hosted-tier ops (this server)

### Files

```
/srv/apps/sandbox-orchestrator/
├── docker-compose.yml       # Traefik labels, Docker socket mount, health check
└── .env                     # MATRX_API_KEY, MATRX_HOST_TIER=hosted, MATRX_DOCKER_NETWORK=proxy, …  (chmod 600)
```

API key is also recorded in `/srv/.credentials` as `SANDBOX_ORCHESTRATOR_HOSTED_API_KEY`.

### Common operations

```bash
# Tail logs
docker logs matrx-orchestrator --tail 50 -f

# Restart (no rebuild)
cd /srv/apps/sandbox-orchestrator && docker compose restart

# Rebuild after a code change in the orchestrator source
cd /srv/projects/matrx-sandbox/orchestrator && docker build -t matrx-orchestrator:latest .
cd /srv/apps/sandbox-orchestrator && docker compose up -d --force-recreate

# Health
curl https://orchestrator.dev.codematrx.com/health
curl https://orchestrator.dev.codematrx.com/api-surface | jq

# List currently-spawned sandboxes (orchestrator's own view)
curl -H "X-API-Key: $KEY" https://orchestrator.dev.codematrx.com/sandboxes

# List sandbox containers from Docker's perspective
docker ps --filter label=matrx.sandbox_id --format "table {{.Names}}\t{{.Status}}"
```

### Sandbox image rebuild + recreate the starter pool

When the sandbox image (`matrx-sandbox:core` / `matrx-sandbox:local`) changes — e.g. you edited the in-container `matrx_agent` daemon, added a tool to the Dockerfile, or fixed `entrypoint-local.sh`:

```bash
# Rebuild the core image
cd /srv/projects/matrx-sandbox/sandbox-image && docker build -t matrx-sandbox:core .

# Rebuild the local variant (adds ttyd on top of core)
cd /srv/projects/matrx-sandbox/sandbox-local && docker build -t matrx-sandbox:local .

# Recreate the starter-pool containers (sandbox-1..5)
docker compose up -d --force-recreate

# Verify the daemon is listening
docker exec sandbox-1 netstat -tlnp | grep :8000
docker exec sandbox-1 curl -sS http://127.0.0.1:8000/docs | head -5
```

Existing dynamically-spawned sandboxes (`sbx-*`) keep their old image until destroyed and recreated — this is by design so in-flight user sessions aren't disrupted. Force a refresh by destroying them via the API.

### Killing a zombie sandbox

If `/sandboxes` lists a sandbox the orchestrator can't talk to:

```bash
# 1. Confirm the container is gone (or stuck)
docker ps -a --filter name=$SANDBOX_ID

# 2. Force-stop via the orchestrator (preferred — keeps state in sync)
curl -X DELETE -H "X-API-Key: $KEY" \
  "https://orchestrator.dev.codematrx.com/sandboxes/$SANDBOX_ID?graceful=false"

# 3. If the orchestrator is unhappy, kill the container directly
docker rm -f $SANDBOX_ID

# 4. Restart the orchestrator (clears in-memory state when using memory store)
cd /srv/apps/sandbox-orchestrator && docker compose restart
```

### Capacity

This host has **32 GB RAM / 8 cores / 388 GB disk**. With the default 4 GB per sandbox, that's a hard ceiling of ~6 concurrent hosted sandboxes before swap pressure. The orchestrator does **not** enforce capacity today — it just tries to run `docker run` and fails if Docker rejects it. Frontends should either:

- Track concurrency themselves (per-user max + show "host at capacity" when `GET /sandboxes` count is high).
- Wait for a future `MATRX_MAX_SANDBOXES` enforcement.

To temporarily reduce per-sandbox footprint: edit `MATRX_CONTAINER_MEMORY_LIMIT` in `/srv/apps/sandbox-orchestrator/.env` (defaults to `4g`) and restart. New sandboxes get the new limit; existing ones keep theirs.

### Switching the metadata store to Postgres

The hosted orchestrator runs in-memory today. To move to Postgres on the shared instance:

1. Pick a database name (e.g. `sandbox_orchestrator_hosted`) and create it on the shared cluster.
2. Run [migrations/001_create_sandboxes.sql](../orchestrator/migrations/001_create_sandboxes.sql) and [migrations/002_add_tier_template_columns.sql](../orchestrator/migrations/002_add_tier_template_columns.sql) — but **strip the FK references** to `auth.users(id)` and `projects(id)` first (those are Supabase tables that don't exist locally). A hosted-tier-specific migration file is the cleanest approach.
3. In `/srv/apps/sandbox-orchestrator/.env`, set:
   ```
   MATRX_SANDBOX_STORE=postgres
   MATRX_DATABASE_URL=postgresql://matrx:<password>@postgres:5432/sandbox_orchestrator_hosted
   ```
4. `docker compose restart` — orchestrator will pick up the new store.

### Rotating the hosted-tier API key

```bash
# Generate a new key
NEW_KEY=$(openssl rand -hex 32)

# Edit /srv/apps/sandbox-orchestrator/.env — replace MATRX_API_KEY=…
# Edit /srv/.credentials — update SANDBOX_ORCHESTRATOR_HOSTED_API_KEY=…
# Tell the frontend team — they need to update MATRX_HOSTED_ORCHESTRATOR_API_KEY in Vercel.

cd /srv/apps/sandbox-orchestrator && docker compose restart
```

Old in-flight sessions continue (sandbox containers don't re-validate the key); only new orchestrator API calls need the new key. There is no key history — set, rotate, communicate.

---

## EC2-tier ops

### Deploy a new orchestrator version

The CI deploys on push to `main` via [`.github/workflows/deploy.yml`](../.github/workflows/deploy.yml). To trigger manually:

```bash
# From your laptop or this server (gh CLI authenticated)
gh workflow run deploy.yml --repo armanisadeghi/matrx-sandbox

# Or push a no-op commit
cd /srv/projects/matrx-sandbox
pnpm ship "trigger deploy"   # bumps patch, commits, pushes → GHA picks it up

# Watch the run
gh run list --workflow=deploy.yml --limit 1
gh run watch
```

The pipeline:
1. `pytest` on orchestrator/
2. ECR login + build + push of sandbox-image and orchestrator images
3. SSM command to EC2: pull, tag, restart `matrx-orchestrator.service`
4. Health check loop (30 attempts × 2 s) against `http://<EC2_PUBLIC_IP>:8000/health`

### Verify EC2 has the latest code

```bash
curl http://54.144.86.132:8000/                # version field
curl http://54.144.86.132:8000/api-surface     # full route list (added in v0.2.0)
```

If `version` is older than the latest tag in `git log --oneline`, the pipeline either failed or didn't trigger. Check:

```bash
gh run list --workflow=deploy.yml --limit 5
gh run view <run-id>
```

### Required GitHub Secrets

For the deploy pipeline to work, the repo needs:

| Secret | Purpose |
|---|---|
| `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` | IAM user with ECR + SSM permissions |
| `ECR_REPO_URI` | Base ECR URI for image pushes |
| `EC2_INSTANCE_ID` | Target EC2 instance for SSM |
| `EC2_PUBLIC_IP` | For the post-deploy health check |
| `EC2_SSH_PRIVATE_KEY` | Backup access (SG allows only the home IP for SSH) |

---

## Where the secrets live

| Tier | Secret | Where stored | Used by |
|---|---|---|---|
| EC2 orchestrator | `MATRX_API_KEY` | EC2 systemd unit env / secrets manager | Frontend `MATRX_ORCHESTRATOR_API_KEY` |
| EC2 orchestrator | `MATRX_DATABASE_URL` (Supabase) | EC2 systemd env | EC2 only |
| Hosted orchestrator | `MATRX_API_KEY` | `/srv/apps/sandbox-orchestrator/.env` (chmod 600) + `/srv/.credentials` as `SANDBOX_ORCHESTRATOR_HOSTED_API_KEY` | Frontend `MATRX_HOSTED_ORCHESTRATOR_API_KEY` |
| GHA pipeline | AWS keys + EC2 ID | GitHub repo Secrets | `.github/workflows/deploy.yml` |

---

## AI Dream ↔ Sandbox integration

Sandboxes can act on behalf of users against AI Dream's cloud_files (`cld_files`) backend, surfacing each user's uploaded files at `/home/agent/cloud-files/` for native shell-tool access by agents.

**Wiring it up** — set in `/srv/apps/sandbox-orchestrator/.env`:
```
MATRX_AIDREAM_URL=https://api.aidream.example.com
MATRX_AIDREAM_SERVICE_TOKEN=<shared with AI Dream's AIDREAM_SANDBOX_SERVICE_TOKEN>
```
Then `cd /srv/apps/sandbox-orchestrator && docker compose restart`. New sandboxes will auto-sync at startup.

**Verifying it from the orchestrator:**
```bash
curl https://orchestrator.dev.codematrx.com/ | jq .integrations.aidream
# expect: { "configured": true, "url": "https://api.aidream.example.com" }
```

**Verifying it from inside any sandbox:**
```bash
mtx whoami            # aidream.configured: true
mtx files ls          # lists user's cld_files
```

Spec for what AI Dream needs to expose: **[AIDREAM_INTEGRATION.md](AIDREAM_INTEGRATION.md)**.

---

## Persistence — what's saved, where, and how to inspect

User data persists across sandbox lifecycle. Two storage backends, depending on tier:

| Tier | Backend | Path |
|---|---|---|
| EC2 | S3 prefix per user | `s3://matrx-sandbox-storage-prod-2024/users/{user_id}/{hot,cold}/` |
| Hosted | Per-user Docker volume | `matrx-user-<uid>` mounted at `/home/agent` |

Both tiers also run an in-container persistence module that:
- Writes `~/.matrx/session.json` every 5 min and on shutdown
- Auto-stashes dirty git repos to `matrx/auto-stash/<ts>` branches on shutdown (pushed when creds work)
- Renders `~/.matrx/session-report.md` on startup with a "what was preserved / what was lost" report

**Inspecting a user's persistence:**
```bash
# Hosted tier — Docker
docker volume ls --filter label=matrx.user_id=<uuid>
docker run --rm -v matrx-user-<uuid>:/home/agent alpine du -sh /home/agent

# Either tier (via orchestrator API)
curl -H "X-API-Key: $KEY" https://<orch>/users/<uuid>/persistence | jq
```

**Wiping a user's data (destructive):**
```bash
# Hosted tier only — refuses if any sandbox of theirs is running
curl -X DELETE -H "X-API-Key: $KEY" https://<orch>/users/<uuid>/volume
# EC2 tier — manual aws s3 rm against users/<uuid>/ prefix
```

**Inside a sandbox** (the user's own POV):
```bash
cat ~/.matrx/session-report.md     # what was restored / lost
cat ~/.matrx/session.json | jq     # full manifest
git stash list                     # see auto-stashed work
```

Full design + decisions: [PERSISTENCE_PLAN.md](PERSISTENCE_PLAN.md).

---

## Recovering EC2 from a stale or stuck state

Two things can go wrong on EC2 at the same time, both of which happened on 2026‑04‑26:

### 1. Disk full (deploy step `Deploy to EC2 via SSM` fails)

Symptom: GHA log shows `failed to register layer: ... no space left on device` partway through `docker pull`.

Recovery via Session Manager (no SSH needed):
1. EC2 console → find the instance by IP → **Connect** → **Session Manager** tab.
2. `df -h /` (confirm > 90% Use%).
3. `sudo docker system prune -af --volumes`.
4. `df -h /` (confirm space freed).
5. Re-trigger: `gh workflow run deploy.yml --repo armanisadeghi/matrx-sandbox` (run from anywhere with `gh` auth).

The deploy pipeline (post‑v0.2.0) auto-prunes before each pull, so this should be self-healing going forward. If it still happens, the prune isn't working (maybe `docker` permissions issue) — investigate the SSM command output.

### 2. Orchestrator code on EC2 is stale even though deploy "succeeded"

Symptom: `curl http://54.144.86.132:8000/` shows an old version. `/api-surface` returns 401 (old middleware blocked it) or 404.

Root cause (legacy): pre-v0.2.0 deploy.yml only built/pushed Docker images and called `systemctl restart`. But the systemd unit runs Python from `/home/ec2-user/orchestrator/` — not from any Docker container. So `restart` reloaded the same old on-disk code over and over.

Manual recovery:
```bash
# In Session Manager:
sudo bash -c '
  set -e
  cp -a /home/ec2-user/orchestrator /home/ec2-user/orchestrator.backup-$(date +%Y%m%d)
  rm -rf /tmp/matrx-sandbox-recover
  git clone --depth 1 https://github.com/armanisadeghi/matrx-sandbox.git /tmp/matrx-sandbox-recover
  find /home/ec2-user/orchestrator -mindepth 1 -maxdepth 1 ! -name ".*" -exec rm -rf {} +
  cp -a /tmp/matrx-sandbox-recover/orchestrator/. /home/ec2-user/orchestrator/
  chown -R ec2-user:ec2-user /home/ec2-user/orchestrator
  rm -rf /tmp/matrx-sandbox-recover
  su - ec2-user -c "cd /home/ec2-user/orchestrator && /usr/bin/python3.11 -m pip install --user -e \".[dev]\""
  systemctl restart matrx-orchestrator
  sleep 4
  curl -sS http://localhost:8000/
  curl -sS http://localhost:8000/api-surface | python3 -c "import sys,json; d=json.load(sys.stdin); print(\"routes=\",len(d.get(\"routes\",[])),\"version=\",d.get(\"version\"))"
'
```

Verify `/` shows the expected version and `/api-surface` returns ≥23 routes.

The deploy pipeline (post‑v0.2.0) does this automatically on every push, so this is only needed for one-off recovery if the pipeline itself breaks.

### Tier env var

The orchestrator reports `tier: null` unless `MATRX_HOST_TIER=ec2` is set. Drop-in:
```bash
sudo mkdir -p /etc/systemd/system/matrx-orchestrator.service.d
echo -e '[Service]\nEnvironment=MATRX_HOST_TIER=ec2' | sudo tee /etc/systemd/system/matrx-orchestrator.service.d/tier.conf
sudo systemctl daemon-reload
sudo systemctl restart matrx-orchestrator
```

---

## Monitoring — first place to look

For day-to-day "is the sandbox infra healthy" the **first stop is the matrx-frontend admin panel**:

> **`/administration/sandbox-infra`**

It auto-refreshes every 30s and shows for each tier:

- Health status, version, route count (catches stale deploys live).
- Disk pressure bar with red threshold at 90% (catches the Apr 2026 disk-full silently before deploys fail).
- Memory pressure bar (catches sandbox capacity exhaustion).
- CPU + load averages.
- Sandboxes-in-DB vs Docker-containers-running drift detector.
- Latest 5 GHA `deploy.yml` runs with status icons + commit SHA + actor.
- One-click "Trigger deploy" button (enabled when `MATRX_SANDBOX_GH_TOKEN` is set on the frontend server).

The panel is read-only by default — it just hits the orchestrators' new `GET /system` endpoint and the GitHub API. Triggering a deploy requires `MATRX_SANDBOX_GH_TOKEN` (a PAT with `actions:write` on `armanisadeghi/matrx-sandbox`) configured in the matrx-frontend Vercel env.

CLI alternatives if the frontend is down:
```bash
# Per-tier disk + memory + container counts
curl -H "X-API-Key: $KEY" https://orchestrator.dev.codematrx.com/system | jq
curl -H "X-API-Key: $EC2_KEY" http://54.144.86.132:8000/system | jq

# Latest deploys
gh run list --workflow=deploy.yml --repo armanisadeghi/matrx-sandbox --limit 5

# Trigger a fresh deploy
gh workflow run deploy.yml --repo armanisadeghi/matrx-sandbox
```

---

## What to do when…

| Symptom | Likely cause | Fix |
|---|---|---|
| `https://orchestrator.dev.codematrx.com/health` 502s | `matrx-orchestrator` container down | `docker logs matrx-orchestrator`; if crashed, `docker compose up -d` |
| EC2 `/openapi.json` shows fewer routes than expected | Catchall proxy routes don't appear in OpenAPI by design — use `/api-surface` instead. If `/api-surface` is also missing, the deploy is stale (see "Recovering EC2" above). |
| EC2 `/` shows old version after deploy "succeeded" | Pre‑v0.2.0 deploy never updated on-disk code (it only restarted with the same files). Use the manual recovery in "Recovering EC2" above; it should self-heal on the next deploy. |
| `POST /sandboxes` succeeds on EC2 but fs/git/pty proxies 502 | In-container daemon not running | EC2: SSM into the host, check `docker exec <sbx> ss -tlnp \| grep 8000`. If missing, the sandbox image is stale — rebuild and redeploy. |
| Hosted `/exec` works but `/fs/list` 502s | Spawned sandbox is on the wrong network — orchestrator can't reach `<container_ip>:8000` | Confirm `MATRX_DOCKER_NETWORK=proxy` in the orchestrator .env, and that the sandbox image inherits this via the orchestrator's `network=` argument |
| `/extend` returns 200 but `expires_at` doesn't change | Pre-v0.2.0 orchestrator (stub still in place) | Redeploy with the latest image |
| In-memory store loses sandboxes on orchestrator restart | Expected (in-memory); switch to Postgres per "Switching the metadata store" above | |
| Traefik 404 on `orchestrator.dev.codematrx.com` | DNS not resolving or Traefik labels missing | `dig orchestrator.dev.codematrx.com` (must point at `77.37.62.64`); `docker inspect matrx-orchestrator \| grep traefik` |
