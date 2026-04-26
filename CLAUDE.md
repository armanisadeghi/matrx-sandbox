# Matrx Sandbox

On-demand, isolated Unix sandboxes for AI agent execution. Each sandbox is a Docker container that *appears as a dedicated machine* to the agent — full shell, filesystem, browser, internet. Production runs on EC2; the local variant runs on this dev server.

Already deeply documented. Read the right doc for the question:

| Question | Read |
|---|---|
| End-to-end architecture, storage tiers, lifecycle, deploy pipeline | [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) |
| Code review notes (40 issues, may or may not be current) | [docs/CODE_REVIEW.md](docs/CODE_REVIEW.md) |
| Repository layout in detail | [docs/DIRECTORY_STRUCTURE.md](docs/DIRECTORY_STRUCTURE.md) |
| HTTP API surface for client integrations | [SANDBOX_CLIENT_GUIDE.md](SANDBOX_CLIENT_GUIDE.md) |
| Roadmap of API features being built/wished | [SANDBOX_API_WISHLIST.md](SANDBOX_API_WISHLIST.md) |
| User-facing quickstart + deployment | [README.md](README.md) |
| Adding tools/SDKs to the sandbox image | [sandbox-image/ADDING_UTILITIES.md](sandbox-image/ADDING_UTILITIES.md) |
| Local-only sandbox dev (this server) | [sandbox-local/ROADMAP.md](sandbox-local/ROADMAP.md), [sandbox-local/TESTING.md](sandbox-local/TESTING.md) |

This file is **orientation** — what each piece is, where it runs, and what's specific to running it on this dev server.

---

## Two Deployments You Need to Distinguish

| Deployment | Where | What runs | Trigger |
|---|---|---|---|
| **Production** | EC2 | Orchestrator (FastAPI) + sandbox containers it spawns on demand | Push to `main` → GitHub Actions → ECR build → SSM deploy |
| **Local on this dev server** | Here in `/srv` | Pre-spawned `sandbox-1` … `sandbox-5` containers via [sandbox-local/docker-compose.yml](sandbox-local/docker-compose.yml). Each one routed by Traefik to `sandbox-N.dev.codematrx.com` (web terminal via ttyd on port 7681). **No orchestrator running here** — these are static, long-lived sandboxes for testing. | `cd sandbox-local && docker compose up -d` |

Plus, separately, there's a **Ship instance** for this project at `matrx-sandbox.dev.codematrx.com` (container `matrx-sandbox`, image `matrx-ship:latest`) — that's just version tracking via Matrx Ship, **not** the sandbox runtime. Don't confuse them.

---

## The Three Code Surfaces

### 1. [sandbox-image/](sandbox-image/) — The container itself

Multi-arch Docker image (Ubuntu 22.04 + Python 3.11 + Node 20 + Chromium + Playwright + AWS CLI v2 + custom Matrx SDK). What an agent sees as "the machine."

- [Dockerfile](sandbox-image/Dockerfile) — base image. mountpoint-s3 (FUSE for cold storage) installed only on x86_64.
- [scripts/](sandbox-image/scripts/) — `entrypoint.sh`, `hot-sync.sh`, `cold-mount.sh`, `shutdown.sh`, `healthcheck.sh`. Lifecycle is documented in detail in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).
- [sdk/](sandbox-image/sdk/) — `matrx_agent` Python package (`client.py`) installed inside every sandbox.
- **Storage tiers (important):**
  - **Hot** = `/home/agent/`, eagerly synced from `s3://bucket/users/{uid}/hot/` at startup, synced back on graceful shutdown. Small, frequent files.
  - **Cold** = `/data/cold/`, lazily mounted via FUSE (mountpoint-s3). Large, rare files. **x86_64 only.**

### 2. [orchestrator/](orchestrator/) — The control plane

FastAPI app that creates/destroys sandbox containers and tracks them in Postgres. **This is what the production EC2 host runs.**

- [orchestrator/main.py](orchestrator/main.py) — FastAPI entry, lifespan hooks (validates S3 bucket on startup, closes Docker client + store on shutdown).
- [orchestrator/sandbox_manager.py](orchestrator/sandbox_manager.py) — Docker container lifecycle.
- [orchestrator/store.py](orchestrator/store.py) — `SandboxStore` ABC + `InMemorySandboxStore` (dev) + `PostgresSandboxStore` (prod, Supabase with RLS per `user_id`).
- [orchestrator/storage.py](orchestrator/storage.py) — S3 helpers.
- [orchestrator/middleware/](orchestrator/middleware/) — `auth.py` (API key via `X-API-Key`), `request_logging.py`.
- [orchestrator/routes/](orchestrator/routes/) — `sandboxes.py` (CRUD + exec + heartbeat + complete/error), `health.py`.

The route handlers proxy a much richer API into a `matrx_agent` daemon running inside each container — fs operations, PTY (WebSocket), git workflows, processes, ports. See [SANDBOX_CLIENT_GUIDE.md](SANDBOX_CLIENT_GUIDE.md) for the full surface (note: not all of it may be implemented yet — cross-check before relying on an endpoint).

### 3. [infra/](infra/) — Terraform for AWS

Provisions the production EC2 host, S3 buckets, IAM roles (incl. SSM permissions for keyless deploys). Not used to provision *this* dev server — that's a separate Matrx-Ship-bootstrapped machine.

---

## Sandbox-Local (this server's variant)

[sandbox-local/](sandbox-local/) is a separate, simpler deployment that runs **on this `/srv` host** without orchestrator/S3/Postgres. It pre-spawns five long-lived sandbox containers (`sandbox-1` … `sandbox-5`) and exposes each via Traefik as a web terminal.

- Image: `matrx-sandbox:local`, built locally from [sandbox-local/Dockerfile](sandbox-local/Dockerfile) which extends `matrx-sandbox:core` (built from [sandbox-image/](sandbox-image/)).
- Resource limits per sandbox: 2 CPUs, 4 GB RAM.
- Persistent home volume per sandbox (`sandbox-N-home`).
- Web terminal via ttyd on port 7681, routed by Traefik with TLS.
- **Use case:** quick agent shells, browser-accessible, no orchestrator overhead. Good for manual testing or hand-driven agent sessions.

To rebuild and restart:
```bash
cd /srv/projects/matrx-sandbox/sandbox-image && docker build -t matrx-sandbox:core .
cd /srv/projects/matrx-sandbox/sandbox-local && docker build -t matrx-sandbox:local . && docker compose up -d
```

---

## CI/CD (production)

`.github/workflows/deploy.yml` — push to `main` triggers:
1. `pytest` on orchestrator tests.
2. ECR login + build/push of sandbox + orchestrator images.
3. AWS SSM command to EC2 (no SSH — security group blocks GHA runner IPs anyway).
4. Health check loop (30 attempts × 2s) until `/health` returns OK.

GitHub Secrets required: `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `ECR_REPO_URI`, `EC2_INSTANCE_ID`, `EC2_PUBLIC_IP`, `EC2_SSH_PRIVATE_KEY` (backup access).

---

## API Surface (Quick Reference)

Public orchestrator endpoints (`X-API-Key` required except `/health`):

| Method | Path | Purpose |
|---|---|---|
| POST | `/sandboxes` | Create a sandbox |
| GET | `/sandboxes` | List sandboxes |
| GET | `/sandboxes/{id}` | Get sandbox details |
| POST | `/sandboxes/{id}/exec` | Execute a command |
| DELETE | `/sandboxes/{id}` | Destroy a sandbox |
| POST | `/sandboxes/{id}/heartbeat` | Agent liveness ping |
| POST | `/sandboxes/{id}/complete` | Agent signals success |
| POST | `/sandboxes/{id}/error` | Agent signals failure |
| GET | `/health` | Orchestrator health |

The richer fs / pty / git / processes / ports surface is documented in [SANDBOX_CLIENT_GUIDE.md](SANDBOX_CLIENT_GUIDE.md) — those route into the in-container `matrx_agent` daemon.

---

## Working Here

- **`mountpoint-s3` is x86_64 only.** Building the sandbox image on Apple Silicon natively skips cold-mount install. For x86_64 cross-build: `docker buildx build --platform linux/amd64 -t matrx-sandbox:latest sandbox-image/`.
- **Sandbox containers need `SYS_ADMIN` cap and `/dev/fuse`** (see [sandbox-local/docker-compose.yml](sandbox-local/docker-compose.yml)). FUSE mounting requires this. On EC2 these are granted via Docker run flags.
- **Cold writes can be lost on hard crash.** FUSE flushes on graceful shutdown only. Treat `/data/cold/` as eventually-consistent.
- **Sandbox images are large** — ~2.9 GB. Image builds are slow; cache aggressively. Prefer modifying the SDK or scripts over rebuilding the base.
- **The store has two implementations.** Tests use `InMemorySandboxStore`; prod uses `PostgresSandboxStore` against Supabase with RLS. Don't add fields to one without the other.
- **API key auth is enforced by middleware** — `MATRX_API_KEY` unset = unauthenticated mode (with a startup warning). Don't run that in production.
- **The `matrx-sandbox` Ship instance ≠ the sandbox runtime.** Updates to this project's code don't auto-reach the EC2 production deployment — that's a separate `git push origin main` → GHA pipeline.

---

## Tasks-In-Flight Documents

These TODO/planning docs in the repo root may or may not be current — read with skepticism:

- [ARMAN_TASKS.md](ARMAN_TASKS.md) — manual operator tasks, AWS commands, image customization.
- [LOCAL_AGENT_TASKS.md](LOCAL_AGENT_TASKS.md) — browser agent task list (marked complete).
- [CLAUDE_CODE_AGENT_TASKS.md](CLAUDE_CODE_AGENT_TASKS.md) — Claude Code agent task list.
- [MULTI_IMAGE_CONCEPT.md](MULTI_IMAGE_CONCEPT.md) — design exploration for multi-image sandboxes.
- [SANDBOX_API_WISHLIST.md](SANDBOX_API_WISHLIST.md) — desired API additions.
