# Matrx Sandbox

**Purpose of this file (per the [CLAUDE.md charter](/Users/armanisadeghi/code/common-docs/policies/claude-md-charter.md)):** you are doing sandbox work — the container image and/or the orchestrator for isolated agent machines on AWS (EC2 + hosted tiers). This file carries the sandbox-specific rules, the confusions that actually bite here, and pointers to the shared systems this repo integrates with. It does NOT carry feature detail (the docs table below does), API surfaces, deploy runbooks, or platform-rule bodies — those live in their canonical docs, one-liner + link here. Adding a line requires: "would removing it cause an agent to make a mistake in THIS repo?" Budget: ≤200 lines.

**What this is:** on-demand, isolated Unix sandboxes for AI agent execution. Each sandbox is a Docker container that *appears as a dedicated machine* to the agent — full shell, filesystem, browser, internet. **Two tiers, same orchestrator code:** EC2-tier (ephemeral, S3-backed) and hosted-tier (the `/srv` dev server, persistent volumes, larger workloads). The frontend picks the tier at create time.

## Platform laws (one-liner each — the body lives in the canonical doc)

- **Shared checkout, many concurrent writers — NORMAL, never a finding.** `origin/main` is the only sync point: commit+push as you go in small batches; never tree-wide destructive git (blanket `stash`/`checkout -- .`/`reset --hard`/`clean`/dirty `pull --rebase` — pathspec-scope to your own files); never complain about concurrent editors or request your own PR/branch/worktree. Canonical: [shared-checkout.md](/Users/armanisadeghi/code/common-docs/policies/shared-checkout.md).
- **Mandates — no hardcoded agents.** Anywhere software invokes intelligence, the agent/workflow is chosen live from a UI, never welded into code. Canonical: [mandates/FEATURE.md](/Users/armanisadeghi/code/common-docs/systems/mandates/FEATURE.md).
- **No unapproved schedules.** No automated schedule or interval exists unless Arman approved its exact name and interval in the register; code or an enabled DB row does not grant approval. Register: [operations/scheduled-tasks.md](/Users/armanisadeghi/code/common-docs/operations/scheduled-tasks.md).
- **The user-input law.** `user_input` carries only what a human actually typed, dictated, or said — nothing structured, ever. Canonical: [agent-variable-binding/FEATURE.md](/Users/armanisadeghi/code/common-docs/systems/agent-variable-binding/FEATURE.md) § THE USER-INPUT LAW.
- **Limits are knobs, and agents set them.** Every limit/quota/ceiling/gate is a per-feature admin-adjustable knob with an agent-chosen starting value — never a hardcoded constant, a TBD placeholder, or an absent control. Canonical: [limits-are-knobs-agents-set-them.md](/Users/armanisadeghi/code/common-docs/policies/limits-are-knobs-agents-set-them.md).
- **We don't do legacy.** A replaced system is migrated, repointed, and DELETED — never frozen, never run beside its replacement, never a keep-or-kill question. Canonical: [no-legacy.md](/Users/armanisadeghi/code/common-docs/policies/no-legacy.md).
- **One database, addressed only by URL** (`https://db.matrxserver.com`, never by project ref) — this repo's known exception to the connection-variable rule is below in Working Here.

## Where the detail lives

| Question | Read |
|---|---|
| End-to-end architecture, storage tiers, lifecycle, deploy pipeline | [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) |
| **Day-to-day operations** (deploy, recovery, monitoring, key rotation, URLs, secrets) | [docs/OPERATIONS.md](docs/OPERATIONS.md) |
| HTTP API surface for clients (orchestrator endpoints + in-container fs/pty/git/processes/ports daemon) | [SANDBOX_CLIENT_GUIDE.md](SANDBOX_CLIENT_GUIDE.md) — cross-check before relying on an endpoint; not all of it may be implemented |
| User data persistence (what's saved, where, session-report, auto-stash) | [docs/PERSISTENCE_PLAN.md](docs/PERSISTENCE_PLAN.md) |
| AI Dream ↔ Sandbox integration (cloud-files bridge, `mtx` CLI, service-token auth) | [docs/AIDREAM_INTEGRATION.md](docs/AIDREAM_INTEGRATION.md) |
| Cloud-files replica implementation (watcher, replay, system-path boundary, retries) | [sandbox-image/sdk/matrx_agent/cloud_sync/FEATURE.md](sandbox-image/sdk/matrx_agent/cloud_sync/FEATURE.md) |
| Zero-drift migration (version stamping, drift detection, safe image swap, auto-migrate) | [docs/ZERO_DRIFT.md](docs/ZERO_DRIFT.md) |
| Adding tools/SDKs to the sandbox image | [sandbox-image/ADDING_UTILITIES.md](sandbox-image/ADDING_UTILITIES.md) |
| Repository layout in detail | [docs/DIRECTORY_STRUCTURE.md](docs/DIRECTORY_STRUCTURE.md) |
| Local-only sandbox dev on the `/srv` host | [sandbox-local/ROADMAP.md](sandbox-local/ROADMAP.md), [sandbox-local/TESTING.md](sandbox-local/TESTING.md) |
| AI Work Hub provider runtime (product plan this repo hosts the runtime for) | [ai-work-hub PLAN](/Users/armanisadeghi/code/common-docs/projects/ai-work-hub/PLAN.md) |
| Persistent Cloud Browser (sandbox-sidecar browser plan) | [persistent-cloud-browser PLAN](/Users/armanisadeghi/code/common-docs/projects/persistent-cloud-browser/PLAN.md) |
| Live infra dashboard (recommended first stop) | `/administration/sandbox-infra` in matrx-frontend |

**Terminology:** canonical platform vocabulary (Server / Project / Deployment / Sandbox / Template / Image; Manager vs Orchestrator vs Deploy) is [matrx-ship NAMING.md](../matrx-ship/NAMING.md) — where this file differs, NAMING.md wins. "Tier" = where a Sandbox runs (`ec2`/`hosted`); the `matrx-sandbox` *container* on the dev server is a Ship Deployment (version tracking), not a Sandbox.

## The classic confusion: three deployments + a name collision

| Deployment | Where | What | Deploy trigger |
|---|---|---|---|
| **EC2 tier** (`tier: "ec2"`) | EC2 | Orchestrator (FastAPI) + on-demand sandbox containers. S3 hot/cold + Supabase Postgres. | Push to `main` → GitHub Actions → ECR build → SSM deploy → health-check loop ([.github/workflows/deploy.yml](.github/workflows/deploy.yml)) |
| **Hosted tier** (`tier: "hosted"`) | `/srv` dev server | Orchestrator at `https://orchestrator.dev.codematrx.com` + spawned sandboxes. Per-user Docker volumes (`matrx-user-<uid>` at `/home/agent`) survive container destroy. Same Postgres `sandbox_instances` table as EC2 — one source of truth. Boot + 60s liveness reconcile from `docker ps` ([docs/ARCHITECTURE.md](docs/ARCHITECTURE.md#status-reconciliation-boot--periodic-liveness)). | Push to `main` — the host **self-deploys** via `matrx-hosted-deploy.timer`, a 2-min poller running [scripts/deploy-hosted.sh](scripts/deploy-hosted.sh). ⚠️ the poller `git reset --hard`s that checkout every tick — commit+push as you go, or `systemctl stop matrx-hosted-deploy.timer` while editing there. |
| **Static "starter pool"** ⚠️ deprecated | `/srv` dev server | Hard-coded `sandbox-1`…`sandbox-5` from [sandbox-local/docker-compose.yml](sandbox-local/docker-compose.yml), per-slot (not per-user) volumes. Retire once the frontend create-flow is ready. | rebuilt by the same deploy poller |

Name collision on the dev server — three things called "sandbox":
- `matrx-sandbox` container → Ship instance, **version tracking only**, not the runtime. Pushing here does NOT auto-reach EC2 production either — that's the separate GHA pipeline.
- `matrx-orchestrator` → hosted-tier orchestrator (spawns/manages sandboxes).
- `sandbox-1`…`sandbox-5` → deprecated starter-pool containers.

## The three code surfaces

1. **[sandbox-image/](sandbox-image/)** — the container (Ubuntu 22.04, Python 3.11, Node 20, Chromium/Playwright, AWS CLI, `matrx_agent` SDK in [sdk/](sandbox-image/sdk/), lifecycle scripts in [scripts/](sandbox-image/scripts/)). Storage: **hot** = `/home/agent/`, eagerly synced with `s3://bucket/users/{uid}/hot/` at start/graceful stop; **cold** = `/data/cold/`, lazy FUSE mount via mountpoint-s3.
2. **[orchestrator/](orchestrator/)** — FastAPI control plane: [main.py](orchestrator/orchestrator/main.py) (lifespan, `/drift`, `/migrate-all`), [sandbox_manager.py](orchestrator/orchestrator/sandbox_manager.py) (container lifecycle), [store.py](orchestrator/orchestrator/store.py) (`SandboxStore` ABC → InMemory/Postgres), [storage.py](orchestrator/orchestrator/storage.py) (S3), [middleware/](orchestrator/orchestrator/middleware/) (`X-API-Key` auth), [routes/](orchestrator/orchestrator/routes/). Routes proxy the richer fs/pty/git API into the in-container `matrx_agent` daemon.
3. **[infra/](infra/)** — Terraform for the production EC2 host/S3/IAM. NOT for the `/srv` dev server (that's Matrx-Ship-bootstrapped).

## Working here — the rules that bite

- **🚨 The orchestrator REFUSES TO START on a bad store config — `MATRX_SANDBOX_STORE` has NO default.** Unset, misspelled, or `memory` on a deployed host ⇒ `RuntimeError` at boot naming the variable, accepted values (`postgres` | `memory`), and the fix ([config.py `resolve_sandbox_store`](orchestrator/orchestrator/config.py), enforced in [store.py `create_store`](orchestrator/orchestrator/store.py), resolved first in `main.py`'s lifespan so no `except` swallows it). `memory` is a local-dev/test opt-in only (`MATRX_STAGE=local` or pytest), announced by a boot banner and on `/health`. `is_deployed_host` fails CLOSED: unset/unknown `MATRX_STAGE` counts as deployed. Both deploy scripts pre-flight `postgres` so the refusal lands before the container swap. Guard tests: [test_store_config_guard.py](orchestrator/tests/test_store_config_guard.py). Don't weaken any of this — it replaced a silent `memory` default that lost every `sandbox_instances` row on restart.
- **No silent defaults, generally.** Any config value whose *absence* degrades a deployed host (S3 bucket, hosted-tier AWS creds, AI Dream service token, access-token secret, host tier) is listed in `main.py::_degraded_config_warnings` and screams `DEGRADED CONFIG:` at boot — non-fatal, never silent. New optional-but-consequential setting → add it there.
- **API key auth:** `MATRX_API_KEY` unset = unauthenticated mode (screaming banner on a deployed host, plain warning locally — [middleware/auth.py](orchestrator/orchestrator/middleware/auth.py)). Never run that in production.
- **The store has two implementations** — `InMemorySandboxStore` (tests) and `PostgresSandboxStore` (prod, Supabase, RLS per `user_id`). Don't add fields to one without the other.
- **Package / Implementation Separation — read before touching ANY database or connection config.** One connection, one set of variable names; a different database is a change of VALUES, never a new variable name. ⚠️ This repo's orchestrator predates the rule and reaches the same platform Supabase through its own `MATRX_DATABASE_URL` + `MATRX_SANDBOX_STORE` ([config.py](orchestrator/orchestrator/config.py), [store.py](orchestrator/orchestrator/store.py)) — a known second name for the one connection; don't copy the pattern and don't add a third. Connection strings this repo handles for OTHER services (Coolify/host-managed Postgres, template files) are out of scope. Canonical: [package-vs-implementation.md](/Users/armanisadeghi/code/common-docs/policies/package-vs-implementation.md).
- **`mountpoint-s3` is x86_64 only** — a native Apple Silicon build skips cold-mount install; cross-build with `docker buildx build --platform linux/amd64 …`. Sandbox containers need the `SYS_ADMIN` cap and `/dev/fuse` for it ([sandbox-local/docker-compose.yml](sandbox-local/docker-compose.yml); Docker run flags on EC2).
- **Cold writes can be lost on hard crash** — FUSE flushes on graceful shutdown only; treat `/data/cold/` as eventually-consistent.
- **Images are ~2.9 GB and slow to build** — prefer modifying the SDK or scripts over rebuilding the base; the `/srv` deploy poller rebuilds `matrx-sandbox:core`/`:local` automatically on push (path-diff triggered), manual build is a local-iteration fallback only.

## Tasks-in-flight docs (repo root)

[ARMAN_TASKS.md](ARMAN_TASKS.md), [LOCAL_AGENT_TASKS.md](LOCAL_AGENT_TASKS.md), [CLAUDE_CODE_AGENT_TASKS.md](CLAUDE_CODE_AGENT_TASKS.md), [MULTI_IMAGE_CONCEPT.md](MULTI_IMAGE_CONCEPT.md), [SANDBOX_API_WISHLIST.md](SANDBOX_API_WISHLIST.md) — planning/TODO docs that may or may not be current; read with skepticism.
