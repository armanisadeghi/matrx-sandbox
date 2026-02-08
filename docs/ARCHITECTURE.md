# AI Matrx Ephemeral Sandbox Architecture

## Overview

On-demand, isolated Unix sandboxes for AI agent execution. Each sandbox is a
Docker container on EC2 that looks and feels like a dedicated machine to the AI
model. The agent runs shell commands, browses the web, makes API calls, and
interacts with the filesystem natively — with no awareness that storage is
tiered or that the environment is ephemeral.

## System Components

```
┌─────────────────────────────────────────────────────────────────┐
│                    ai-matrx-admin (Next.js)                     │
│          /sandbox page → API routes → orchestrator              │
└────────────┬────────────────────────────────────────────────────┘
             │  HTTPS (Vercel → EC2:8000)
             ▼
┌─────────────────────────────────────────────────────────────────┐
│                      Orchestration API                         │
│              (FastAPI — sandbox CRUD + health)                 │
│          API key auth · JSON logging · Postgres store          │
└────────────┬──────────────────────────────────────┬────────────┘
             │                                      │
             ▼                                      ▼
┌────────────────────────────┐        ┌──────────────────────────┐
│   Sandbox Lifecycle        │        │   Sandbox Instance Store │
│       Manager              │        │   (Supabase Postgres)    │
│  (create/destroy/          │        │   RLS per user_id        │
│   monitor containers)      │        └──────────────────────────┘
└────────────┬───────────────┘
             │
             ▼
┌─────────────────────────────────────────────────────────────────┐
│                    Docker Container (Sandbox)                   │
│                                                                 │
│  ┌──────────────┐  ┌──────────────┐  ┌───────────────────────┐ │
│  │  Hot Storage  │  │ Cold Storage  │  │   Agent Runtime Env   │ │
│  │  /home/agent/ │  │ /data/cold/   │  │  Python, Node, shell  │ │
│  │  (S3 sync)    │  │ (FUSE+S3)    │  │  Chromium, custom SDK │ │
│  └──────────────┘  └──────────────┘  └───────────────────────┘ │
│                                                                 │
│  ┌──────────────────────────────────────────────────────────┐  │
│  │            Entrypoint / Lifecycle Scripts                 │  │
│  │  startup.sh → hot-sync-down → mount FUSE → ready signal  │  │
│  │  shutdown.sh → hot-sync-up → unmount FUSE → exit          │  │
│  └──────────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────┘
             │                    │
             ▼                    ▼
┌────────────────────┐  ┌────────────────────┐
│   S3 Hot Bucket    │  │  S3 Cold Bucket    │
│  /users/{uid}/hot/ │  │ /users/{uid}/cold/ │
└────────────────────┘  └────────────────────┘
```

## Storage Tiers

### Hot Storage (`/home/agent/`)
- **Contents**: Small, frequently accessed files — markdown, config, agent
  memory, text files, code snippets. Typically < 100 MB per user.
- **Behavior**: On sandbox startup, all hot files are eagerly copied from
  `s3://{bucket}/users/{user_id}/hot/` to `/home/agent/` using `aws s3 sync`.
  On graceful shutdown, the directory syncs back to S3.
- **Why**: The agent reads/writes these files constantly. They must be real
  local files with zero latency. The small size makes full copy feasible.

### Cold Storage (`/data/cold/`)
- **Contents**: Large, infrequently accessed files — videos, datasets, model
  weights, archives. Can be many GB per user.
- **Behavior**: Mounted via a FUSE filesystem (mountpoint-s3). Files appear in
  the directory listing but are only downloaded on first access, then cached
  locally. Writes are flushed back to S3.
- **Why**: Copying GB of data at startup would be slow and wasteful. Lazy
  loading gives the agent seamless access without the cost.
- **Note**: mountpoint-s3 is only available on x86_64. ARM64 builds (e.g. Apple
  Silicon local dev) skip cold mount installation.

### Why Not Just FUSE Everything?
FUSE adds latency on first read and has edge cases with small-file I/O patterns
(stat storms, metadata overhead). Hot files are small and accessed constantly,
so real local copies are faster and more reliable. Cold files are large and
rarely touched, so lazy loading is the right tradeoff.

## Sandbox Lifecycle

```
1. API receives create request (user_id, config)
2. Lifecycle manager pulls sandbox image, creates container
3. Container entrypoint runs:
   a. Hot sync: aws s3 sync s3://bucket/users/{uid}/hot/ /home/agent/
   b. Cold mount: mount FUSE at /data/cold/ → s3://bucket/users/{uid}/cold/
   c. Start agent runtime (or wait for commands)
   d. Send "ready" signal to orchestrator
4. Agent operates for 20 min – 2+ hours
5. Graceful shutdown triggered (API call, timeout, or agent signal):
   a. Hot sync: aws s3 sync /home/agent/ s3://bucket/users/{uid}/hot/
   b. Cold flush: ensure pending writes are flushed, unmount FUSE
   c. Container stops — sandbox marked STOPPED in database (not deleted)
6. On crash/ungraceful shutdown:
   a. Orchestrator detects missing heartbeat
   b. Attempts hot sync from container if still accessible
   c. Marks session as failed, logs state for recovery
```

## Container Image Contents

The base sandbox image includes:
- **OS**: Ubuntu 22.04
- **Languages**: Python 3.11+, Node.js 20 LTS
- **Shell tools**: bash, curl, wget, git, jq, ripgrep, fd-find, htop, tmux
- **Browser**: Chromium (headless) + Playwright
- **AWS CLI v2**: For S3 sync operations (multi-arch)
- **FUSE**: mountpoint-s3 for cold storage (x86_64 only)
- **Python packages**: httpx, requests, aiohttp, pydantic, rich, pandas, numpy, beautifulsoup4
- **Custom SDK**: Matrx Python package (agent utilities, API clients)
- **Security**: Non-root user `agent`, limited capabilities, no host networking

## Persistence Layer

### Sandbox Instance Store
Sandbox metadata is persisted in Supabase Postgres (`sandbox_instances` table):
- **Store abstraction**: `SandboxStore` ABC with `InMemorySandboxStore` (dev) and `PostgresSandboxStore` (prod)
- **RLS**: Row-level security scoped to `auth.uid() = user_id`
- **Triggers**: Auto-set `updated_at` and `expires_at` (2-hour TTL default)
- **Lifecycle**: Sandboxes are marked STOPPED on destroy (not deleted) for audit trail
- **Reconciliation**: Background task syncs Docker container state with DB records

## Networking & Security

- Containers run with `--network=bridge` (isolated network)
- Outbound internet access allowed (agent needs to call APIs, browse web)
- No inbound access from the internet (agent-initiated only)
- AWS credentials passed via IAM instance role (no keys in container)
- Each container gets scoped S3 access to only its user's prefix
- Resource limits: CPU (2 cores), memory (4 GB), disk (20 GB), enforced via
  Docker resource constraints

## CI/CD Pipeline

### GitHub Actions Deploy (`deploy.yml`)
1. **Trigger**: Push to `main` branch (automatic) or manual `workflow_dispatch`
2. **Test job**: Installs orchestrator dev dependencies, runs `pytest`
3. **Deploy job** (only on `main`):
   a. Configures AWS credentials via GitHub Secrets
   b. Logs into ECR
   c. Builds and pushes sandbox + orchestrator images to ECR
   d. Deploys to EC2 via **AWS SSM** (Systems Manager) — no SSH needed
   e. SSM command: pulls images from ECR, tags them, restarts systemd service
   f. Health check loop (30 attempts, 2s interval) confirms orchestrator is healthy

### Why SSM Instead of SSH
- EC2 security group restricts SSH to a single home IP
- GitHub Actions runners use dynamic IPs — can't whitelist them
- SSM uses IAM auth (no open ports needed), is more secure, and works from any runner

### Required GitHub Secrets
| Secret | Purpose |
|--------|---------|
| `AWS_ACCESS_KEY_ID` | IAM user for CI/CD |
| `AWS_SECRET_ACCESS_KEY` | IAM user for CI/CD |
| `ECR_REPO_URI` | ECR repository base URI |
| `EC2_INSTANCE_ID` | Target EC2 instance for SSM |
| `EC2_PUBLIC_IP` | EC2 Elastic IP (for reference) |
| `EC2_SSH_PRIVATE_KEY` | SSH key (backup access) |

## Local Development

### Docker Compose Profiles

**Default (LocalStack)** — no AWS account needed:
```bash
docker compose up
```
Runs orchestrator + LocalStack (fake S3). Good for API development.

**Production profile** — real Postgres + S3:
```bash
docker compose --profile production up orchestrator-prod
```
Uses `.env` file with real AWS/Supabase credentials. Good for end-to-end testing.

### Building Images Locally

```bash
# Sandbox image (~2.9 GB, includes Python, Node, Chromium, etc.)
docker build -t matrx-sandbox:latest sandbox-image/

# Orchestrator image (~686 MB)
docker build -t matrx-orchestrator:latest orchestrator/
```

**Note**: On Apple Silicon (ARM64), the sandbox image builds for `linux/arm64`.
mountpoint-s3 is skipped (x86_64 only). For x86_64 cross-compilation:
```bash
docker buildx build --platform linux/amd64 -t matrx-sandbox:latest sandbox-image/
```

## Scaling Model

- **Phase 1** (current): Single EC2 instance, multiple concurrent sandboxes
  via Docker API. No hard limit enforced in code.
- **Phase 2**: Single EC2 instance with resource-aware scheduling (track CPU/memory
  per sandbox, reject creation when host is at capacity).
- **Phase 3**: Auto-scaling EC2 fleet with placement logic. Each EC2 host runs
  N sandboxes. New hosts spin up when existing ones are at capacity.
- **Phase 4**: ECS Fargate or EKS for fully managed container orchestration.

## Cost Optimization

- Use EC2 Spot Instances for sandbox hosts (70-90% savings).
- Sandbox containers are ephemeral — no idle cost.
- S3 Intelligent-Tiering for cold storage.
- Right-size EC2 instances based on concurrent sandbox count.
- Aggressive container cleanup — no zombie sandboxes.

## Key Design Decisions

1. **Docker on EC2 (not Fargate/EKS initially)**: Simpler, cheaper, faster to
   iterate. FUSE requires privileged mode which is easier on raw EC2 + Docker.
2. **S3 as the single persistence layer**: Simple, cheap, scales infinitely.
   No database for file storage.
3. **Eager hot sync + lazy cold mount**: Best latency profile for agent workloads.
4. **Single S3 bucket with user prefixes**: Simpler than per-user buckets.
   IAM policies scope access per prefix.
5. **Graceful shutdown with fallback**: Always attempt sync. On crash, attempt
   recovery. Accept that some cold writes may be lost on hard crash (FUSE
   limitation).
6. **SSM over SSH for deployment**: More secure (IAM-based, no open ports),
   works with dynamic CI runner IPs, audit trail in CloudTrail.
7. **Supabase Postgres for sandbox registry**: Single source of truth shared
   between orchestrator and admin dashboard. RLS for user isolation.
