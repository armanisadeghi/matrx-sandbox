# Matrx Sandbox — Operations

Operational runbook for the two sandbox tiers. For architecture (storage tiers, lifecycle, deploy pipeline) see [ARCHITECTURE.md](ARCHITECTURE.md). For the HTTP API see [SANDBOX_CLIENT_GUIDE.md](../SANDBOX_CLIENT_GUIDE.md).

Current cross-repository routing and preservation contract: [sandbox STATE](../../common-docs/systems/infrastructure/sandboxes/STATE.md). September 13, 2026 — restoration root corrected public transport, storage and unsafe cleanup guidance. Use the approved deployment path; never restart a control process around an active image-update operation or bypass its deployment lock.

---

## The two tiers

| | EC2 tier | Hosted tier |
|---|---|---|
| Orchestrator URL | `https://sandbox-orchestrator.matrxserver.com` | `https://orchestrator.dev.codematrx.com` |
| Code | `/home/ec2-user/orchestrator/` (native systemd service) | `/srv/projects/matrx-sandbox/orchestrator/` (container build source) |
| Where it lives | EC2 instance, single host | This server (`/srv/apps/sandbox-orchestrator/`) |
| Sandbox storage | Template-specific retained homes; S3/FUSE only where the actual template provides it | Per-user Docker named volumes shared by that user's hosted sandboxes |
| Sandbox image | `matrx-sandbox:latest` (production build, no ttyd) | `matrx-sandbox:local` (adds ttyd for browser shells) |
| Metadata store | Supabase Postgres (`sandbox_instances` table, RLS per user) | Supabase Postgres — the SAME shared `sandbox_instances` table as EC2 (tier-scoped). State survives restarts. |
| Default TTL | 7200 s (2 h), auto-shutdown | 7200 s (extendable; sessions can stay alive indefinitely if pinged) |
| Per-sandbox limits | 2 CPU / 4 GB / 20 GB | Configurable via `resources` field on create; default same as EC2 |
| Deploy mechanism | Push to `main` → GHA → ECR build → SSM → restart on EC2 | Push to `main` → `matrx-hosted-deploy.timer` (2-min host poller) runs `scripts/deploy-hosted.sh` (migrations + health-gate + rollback). GHA SSH is best-effort only. |
| API key | `MATRX_API_KEY` on EC2 | `MATRX_API_KEY` in `/srv/apps/sandbox-orchestrator/.env` (also recorded in `/srv/.credentials`) |

The hosted orchestrator contract gate allows up to three minutes for cold-boot
reconciliation before rollback. The previous one-minute gate repeatedly rolled
back healthy candidates on a fleet of roughly 160 live containers.

Both orchestrators advertise their tier via `GET /` and `GET /api-surface`. A `POST /sandboxes` request whose `tier` doesn't match the orchestrator's `MATRX_HOST_TIER` is rejected with HTTP 400 — there is no cross-tier proxying. Frontends route by reading the sandbox row's `tier` column.

The best-effort hosted GitHub job waits up to 120 minutes because it uses the
same serialized host lock as the poller. The authoritative systemd service has
a three-hour budget, covering both lock wait and a cold core/slim/aidream build.
Successful releases refresh the runner and both systemd units, so the live
timeout policy cannot silently remain pinned to an obsolete checkout.

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

# Rebuild after a code change: NORMALLY AUTOMATIC — commit + push to main and
# the deploy poller rebuilds, runs DB migrations, health-gates, and rolls back
# on failure (journalctl -u matrx-hosted-deploy.service -f to watch).
# Manual fallback only (skips nothing — same script the poller runs):
FORCE=1 bash /srv/projects/matrx-sandbox/scripts/deploy-hosted.sh

# Health
curl https://orchestrator.dev.codematrx.com/health
curl -H "X-API-Key: $KEY" https://orchestrator.dev.codematrx.com/api-surface | jq

# List currently-spawned sandboxes (orchestrator's own view)
curl -H "X-API-Key: $KEY" https://orchestrator.dev.codematrx.com/sandboxes

# List sandbox containers from Docker's perspective
docker ps --filter label=matrx.sandbox_id --format "table {{.Names}}\t{{.Status}}"
```

### Sandbox image rebuild + recreate the starter pool

**Automatic on push to `main`** — the deploy poller rebuilds every changed image variant, records the exact successful live image IDs beside the deployed SHA, and self-heals missing or unexpectedly retagged aliases before any no-op exit. It recreates the starter pool when its image changes. The commands below are a manual fallback for local iteration only:

Fleet Health build markers remain active from the start of each candidate build
through atomic live-tag promotion. A built candidate waiting for another image
is therefore shown as **rebuilding**, not as a permanent missing-image outage;
failed deploys clear their markers immediately.

The poller also rebuilds any live sandbox image older than 14 days, even when
its source path has not changed. That matches Fleet Health's freshness limit,
so an age warning self-heals instead of remaining permanently actionable.

The **aidream template** image tracks aidream's own `main`, which moves many
times an hour, so its rebuild cadence is a knob rather than "every tick that
sees a new commit": `AIDREAM_REBUILD_MIN_INTERVAL_SECONDS` (default 21600 = 6 h,
`0` disables the floor), measured from `AIDREAM_REBUILD_STAMP`
(`/srv/apps/deploy-state/matrx-sandbox.aidream-build-epoch`), stamped when a
build STARTS so a failing build cannot re-fire every two minutes. Without it the
poller ran ~40 six-gigabyte builds in 12 h on 2026-09-14, and every one that
finished stopped and recreated the single-replica live orchestrator (8 edge
interruptions of 2–17 s in that window). A missing or unlabeled image,
`MAX_IMAGE_AGE_SECONDS` freshness, a matrx-sandbox source change, the
freshness-UNKNOWN refusal and `FORCE=1` all ignore the floor; every deferral
logs the knob name and the remedy. Need the template now? `FORCE=1 bash
/srv/projects/matrx-sandbox/scripts/deploy-hosted.sh`.

The promotion takes the shared `deployment` lease with a **bounded wait**:
`DEPLOY_LOCK_WAIT_SECONDS` (default 120, `0` = one non-blocking attempt, the old
behaviour). The same lease is held for a few hundred milliseconds by the
60-second liveness reconcile sweep and for the duration of any admitted sandbox
migration, so a single non-blocking attempt threw away a fully built, fully
verified candidate on roughly two poller ticks in five — each loss rolled back
and waited for the next tick. The wait is bounded on purpose: a genuinely long
migration still defers the promotion, and the deferral message names the knob.

The **bootstrap** path (replacing a pre-barrier orchestrator that predates the
release barrier) now **seizes the locks first and pauses only after**. It used to
`docker pause` the exact live orchestrator and only then discover whether the
seize was possible, so a contended or invalid seize froze a healthy edge for an
attempt that was immediately rolled back. Seizing first is free — a failure
defers the promotion with the orchestrator still serving. What the earlier pause
bought (no new operation admitted between the census and the freeze) is bought
instead by re-censusing the journal's `*.lock` set once the control IS frozen: a
new lock name means the old source admitted an operation in the seize window, and
the promotion defers and unpauses rather than cutting it off. Guards:
`orchestrator/tests/test_hosted_promotion_lock_order.py` (the real bash, faked
docker) and `test_release_hardening.py::test_hosted_promotion_holds_exclusive_peer_of_migration_lock`.

The per-release `MATRX_IMAGE_VERSION` stamp is applied after stable dependency
and source layers in both sandbox Dockerfiles. A new commit SHA therefore does
not invalidate the expensive apt, Playwright, Node, and Python package cache.

Aidream candidates are staged from the immutable source SHA resolved before
the build starts. A newer Aidream push may queue a later rebuild, but it cannot
change or invalidate the source underneath an active long-running build.

**The aidream variant's source tree is byte-exact and nothing may prune it.**
The image certifies its own runtime source: `/etc/aidream-image-sha` must equal
`git -C /opt/aidream-template rev-parse HEAD` with a clean `git status`
(`mtx aidream verify-release`, asserted during the build and before managed
autostart). So neither `build-aidream.sh` nor `Dockerfile.aidream` may delete or
edit a tracked file — `git archive` already stages tracked files only. Deleting
"heavy" tracked dirs (`knowledgebase/`, `.claude/`, `tmp/`, …) to save ~6 MB of
a multi-GB image is what wedged the hosted deploy poller for 20 h on
2026-08-11: every 2-min run died at `[build-aidream] exact source commit
staging failed`, so `matrx-sandbox:aidream` went missing and the release stalled
at the previous SHA. Both failure messages now list the offending paths.

Managed autostart executes `/opt/aidream-template`, not the durable
`/home/agent/aidream` worktree. The template is the immutable server release;
the home checkout belongs to the user and may contain intentional commits or
local edits. Never reset or delete that worktree to repair server drift. Rebuild
and promote an exact image instead. The image creates `/var/log/aidream` for the
non-root server at build time, so a fresh container needs no privileged startup
repair. Hosted aidream containers run with Docker's read-only root filesystem
and drop `SYS_ADMIN` plus `/dev/fuse`; EC2 aidream development boxes keep their
existing writable/FUSE behavior and do not autostart the managed Claude API.
Only the durable hosted user home and explicit runtime tmpfs paths remain
writable. The host-enforced mount boundary stays read-only even through the
interactive agent's passwordless sudo: remount and bind-shadow attacks lack the
required capability. Autostart refuses to run until `findmnt` proves the
template resides on a read-only mount. It executes the fixed root-owned helper
with an inert ephemeral `HOME`, fixed `PATH` and source/stamp paths, `/dev/null`
global Git config, and a fixed privileged-mode Bash that ignores exported
functions and profiles. Shell, Python, Git, dynamic-loader, venv,
and uv injection variables are removed before dispatch. Managed serve invokes
the template venv's Python with isolated mode (`-I`). The image build proves
sudo cannot write source or venv, malicious user `sitecustomize.py` and Git
`core.fsmonitor` hooks do not execute, exact-source verification includes
untracked files, persistent-venv command shims cannot replace launch binaries,
and the separate runtime log directory remains writable. Aidream warm pooling
is prohibited because a pre-owner box cannot preserve this policy and volume.

Both CI and Deploy test checkouts fetch full Git history because the release
hardening suite archives the exact legacy EC2 source SHAs. A shallow checkout
cannot validate that recovery path and will fail before the sandbox SDK suite.

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

Existing dynamically-spawned sandboxes (`sbx-*`) keep their old image during normal deployment. Use the explicit image-update path with its preservation and interruption checks only after the target release passes independent acceptance; consult the current sandbox STATE for open gates. Destroying a sandbox is not a safe image-refresh substitute. Never upgrade the user fleet or interrupt a shared-home sibling to clear drift.

### Killing a zombie sandbox

If `/sandboxes` lists a sandbox the orchestrator can't talk to:

```bash
# 1. Confirm the container is gone (or stuck)
docker ps -a --filter name=$SANDBOX_ID

# 2. Force-stop via the orchestrator (preferred — keeps state in sync)
curl -X DELETE -H "X-API-Key: $KEY" \
  "https://orchestrator.dev.codematrx.com/sandboxes/$SANDBOX_ID?graceful=false"

# 3. If the orchestrator is unavailable, inspect its health and recovery state.
#    Do not bypass lifecycle/migration locks with direct container removal.
#    Retained containers may hold the only copy of user home data.

# 4. Restarting the orchestrator does NOT lose state (Postgres-backed store;
#    boot reconcile + zombie reap resync it against docker ps).
cd /srv/apps/sandbox-orchestrator && docker compose restart
```

### Capacity

This host has **32 GB RAM / 8 cores / 388 GB disk**. With the default 4 GB per sandbox, that's a hard ceiling of ~6 concurrent hosted sandboxes before swap pressure. The orchestrator does **not** enforce capacity today — it just tries to run `docker run` and fails if Docker rejects it. Frontends should either:

- Track concurrency themselves (per-user max + show "host at capacity" when `GET /sandboxes` count is high).
- Wait for a future `MATRX_MAX_SANDBOXES` enforcement.

To temporarily reduce per-sandbox footprint: edit `MATRX_CONTAINER_MEMORY_LIMIT` in `/srv/apps/sandbox-orchestrator/.env` (defaults to `4g`) and restart. New sandboxes get the new limit; existing ones keep theirs.

### Metadata store (already Postgres) & migrations

The hosted store is **already Postgres** — the shared Supabase `sandbox_instances` table both tiers use. Schema migrations live in [orchestrator/migrations/](../orchestrator/migrations/) and are applied **automatically** by `orchestrator.migrate_runner` (tracked in `schema_migrations`): the deploy poller runs it before every orchestrator swap, and the Manager UI's "Rebuild orchestrator" button does too. **Never apply migrations by hand with psql** — add a numbered idempotent file and push.

### Rotating the hosted-tier API key

```bash
# Generate a new key
NEW_KEY=$(openssl rand -hex 32)

# Edit /srv/apps/sandbox-orchestrator/.env — replace MATRX_API_KEY=…
# Edit /srv/.credentials — update SANDBOX_ORCHESTRATOR_HOSTED_API_KEY=…
# Update BOTH consumers or their server-side sandbox arming breaks with 403 'Invalid API key':
#   - frontend: MATRX_HOSTED_ORCHESTRATOR_API_KEY in Vercel
#   - aidream:  SANDBOX_ORCHESTRATOR_API_KEY on the ECS task definition
# (Rotating the EC2 tier instead? Same rule: frontend MATRX_ORCHESTRATOR_API_KEY
#  + aidream SANDBOX_EC2_ORCHESTRATOR_API_KEY. Keys are PER HOST, never shared.)

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
1. Runs the locked orchestrator and sandbox-SDK suites.
2. Builds or reuses immutable commit-SHA images and verifies their embedded revisions.
3. SSM stages a locked venv, fails closed on migrations, and rollback-swaps code plus image tags.
4. Records a SHA-qualified immutable approval while the commit is current `main`, then revalidates that approval (not the moving branch) throughout rollout. This lets an approved release finish if a later push fails checks, while deployed-source ancestry still rejects stale or divergent releases.
5. Authenticates to `/api-surface` and asserts the exact source SHA, filesystem contract, and required proxy routes. Immutable ECR candidates are never republished through a partially mutable alias set.

### Verify EC2 has the latest code

```bash
curl -H "X-API-Key: $KEY" https://sandbox-orchestrator.matrxserver.com/            # source_sha + version
curl -H "X-API-Key: $KEY" https://sandbox-orchestrator.matrxserver.com/api-surface # exact contract
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
| EC2 orchestrator | `MATRX_API_KEY` | EC2 systemd unit env / secrets manager | Frontend `MATRX_ORCHESTRATOR_API_KEY` (Vercel) **and** aidream `SANDBOX_EC2_ORCHESTRATOR_API_KEY` (ECS task env) |
| EC2 orchestrator | `MATRX_DATABASE_URL` (Supabase) | EC2 systemd env | EC2 only |
| Hosted orchestrator | `MATRX_API_KEY` | `/srv/apps/sandbox-orchestrator/.env` (chmod 600) + `/srv/.credentials` as `SANDBOX_ORCHESTRATOR_HOSTED_API_KEY` | Frontend `MATRX_HOSTED_ORCHESTRATOR_API_KEY` (Vercel) **and** aidream `SANDBOX_ORCHESTRATOR_API_KEY` (ECS task env) |
| GHA pipeline | AWS keys + EC2 ID | GitHub repo Secrets | `.github/workflows/deploy.yml` |

---

## AI Dream ↔ Sandbox integration

Sandboxes can act on behalf of users against AI Dream's cloud_files (`cld_files`) backend, surfacing each user's uploaded files at `/home/agent/cloud-files/` for native shell-tool access by agents.

**Wiring it up** — the URL depends on the tier. The hosted tier uses the public
ECS endpoint; the EC2 tier uses the private Route 53 name for the same ECS
service so calls stay inside AWS:
```
# Hosted: https://server.app.matrxserver.com
# EC2:    http://aidream.internal.matrxserver.com
MATRX_AIDREAM_URL=<the tier's endpoint above>
MATRX_AIDREAM_SERVICE_TOKEN=<shared with AI Dream's AIDREAM_SANDBOX_SERVICE_TOKEN>
```
Then `cd /srv/apps/sandbox-orchestrator && docker compose restart`. New sandboxes will auto-sync at startup.

On EC2 the setting lives in the `matrx-orchestrator` systemd environment. The
release script fails before pulling images unless it equals
`http://aidream.internal.matrxserver.com`, checks it again after promotion, and performs a
real `/health/version` canary over that private route. Never point an EC2
orchestrator or the sandboxes it creates at `server.app.matrxserver.com`.

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
| EC2 | Template-specific retained home | Inspect the exact sandbox mounts; a tier label or S3 bucket is not proof of home backup |
| Hosted | Docker volume per (user, ORGANIZATION) | `matrx-user-<uid>-org-<oid>` mounted at `/home/agent` |

Both tiers also run an in-container persistence module that:
- Writes `~/.matrx/session.json` every 5 min and on shutdown
- Auto-stashes dirty git repos to `matrx/auto-stash/<ts>` branches on shutdown (pushed when creds work)
- Renders `~/.matrx/session-report.md` on startup with a "what was preserved / what was lost" report

**Inspecting a user's persistence:**
```bash
# Hosted tier — Docker. One user has one home PER ORGANIZATION.
docker volume ls --filter label=matrx.user_id=<uuid>
docker volume ls --filter label=matrx.organization_id=<org-uuid>
docker run --rm -v matrx-user-<uuid>-org-<org-uuid>:/home/agent:ro alpine du -sh /home/agent

# Either tier (via orchestrator API). organization_id is REQUIRED — nothing
# picks a tenant for you.
curl -H "X-API-Key: $KEY" "https://<orch>/users/<uuid>/persistence?organization_id=<org-uuid>" | jq
```

**Wiping a user's data (destructive):**
```bash
# Hosted tier only — refuses if any container still mounts the volume, even stopped.
# organization_id is required: it names WHICH of that user's homes to destroy.
curl -X DELETE -H "X-API-Key: $KEY" "https://<orch>/users/<uuid>/volume?organization_id=<org-uuid>"
# EC2 has no user-volume wipe endpoint; use the exact sandbox's supported lifecycle action.
```

### Pre-organization per-user volumes (`matrx-user-<uid>`, no `-org-` suffix)

Until 2026-09-17 the hosted home was keyed by user alone. Every hosted sandbox
alive today mounts one of those: 213 of the 226 `running` rows in
`public.sandbox_instances` carry a `persistence_volume` of the form
`matrx-user-<uid>` (created between 2026-07-07 and 2026-08-20; verified by query
on 2026-09-17). The key gained the organization because `/home/agent/cloud-files`
mirrors AI Dream files and the bridge now answers per organization — a home keyed
by user alone mixes tenants, so a file from another of that user's organizations
sits on disk unlisted, unreported and refused 409 on its next edit.

**Those volumes are LEFT IN PLACE — never renamed, never adopted, never deleted**
("unreferenced means unfinished, never deletable").

**The user's FIRST organization home inherits the legacy one, automatically
(2026-09-18).** Leaving the legacy volume mounted nowhere would have meant that
all 213 hosted users' next sandbox opened an empty `/home/agent` while their
projects, repos, scratch, `~/.matrx/session.json` and the durable cloud-sync
queue sat on a volume nothing mounted — a silent loss from the person's seat.
So when `ensure_user_volume` **creates** a user's first `matrx-user-<uid>-org-<oid>`
home and a legacy `matrx-user-<uid>` volume exists, it copies the legacy contents
forward once, in one direction, in a short-lived helper container running the
orchestrator's own image (legacy mounted **read-only** at `/from`, the new home
at `/to`, `cp -a`, no network), BEFORE the sandbox starts.

- The new volume carries the label `matrx.inherited_from=<legacy name>`, so an
  operator can see at a glance which homes were seeded:
  `docker volume ls --filter label=matrx.inherited_from`. The orchestrator also
  logs `HOME_INHERITED volume=… legacy=… user=… organization=…` (and the sandbox
  create logs "home … was seeded from the pre-organization volume …").
- **A failed copy REFUSES the sandbox create.** The half-copied new volume is
  removed, the legacy volume is never modified or deleted, and the log carries
  `HOME_INHERITANCE_FAILED` with the cause. A box never starts on an empty or
  half-copied home. Re-running the create retries the copy from scratch.
- **A user's SECOND organization's home starts EMPTY on purpose.** The home is
  that organization's tenant view of their files, not a second copy of the first
  tenant's drawer. Copying work forward into a later organization is a deliberate
  operator act — the last command in the block below.
- Because the copy runs once, on creation, an inherited home is treated as a
  RETAINED home: memory hydration is skipped and `SANDBOX_MIGRATION=1` is set,
  exactly as for a reused home.

Other consequences an operator must know:

- **Migration does not move a box across homes.** `migrate_sandbox` re-mounts the
  binds the old container had, so a migrated box keeps its legacy volume. Only a
  CREATE produces an org-keyed home — and only a CREATE inherits.
- **The cloud-sync queue lives on the volume, not in the container layer**:
  `/home/agent/.matrx/runtime/cloud-sync-queue.jsonl` (`cloud_sync/queue.py`
  `DEFAULT_PATH`), under the `/home/agent` mount. So do `~/.matrx/session.json`
  and the session report. On an inherited first home they come across with
  everything else, so PENDING events replay; a home for a LATER organization
  starts with an empty queue, and anything still PENDING on the legacy volume
  stays in that file until someone reads it.

Inspect or recover one, read-only first:

```bash
# Find them: the old name has no -org- segment.
docker volume ls --format '{{.Name}}' | grep '^matrx-user-' | grep -v -- '-org-'

# Look inside without mounting it into anything live.
docker run --rm -v matrx-user-<uuid>:/legacy:ro alpine sh -c 'du -sh /legacy; ls -la /legacy'

# Read what the sync queue still held.
docker run --rm -v matrx-user-<uuid>:/legacy:ro alpine \
  cat /legacy/.matrx/runtime/cloud-sync-queue.jsonl

# Which homes were seeded from a legacy volume (first organization only).
docker volume ls --filter label=matrx.inherited_from --format '{{.Name}}'

# Copy work forward into a LATER organization's home — this one is manual and
# deliberate, one direction; the first organization's home did it automatically.
docker run --rm -v matrx-user-<uuid>:/from:ro \
  -v matrx-user-<uuid>-org-<org-uuid>:/to alpine \
  sh -c 'cp -a /from/projects /to/ 2>/dev/null; ls /to'
```

The `DELETE /users/{id}/volume` endpoint cannot reach a legacy volume: it names
`matrx-user-<uid>-org-<oid>`. Removing one is a deliberate `docker volume rm`
that nobody is authorised to run on a user's work without Arman naming it dead.

### The identity marker (`/etc/matrx/bridge-env.FAILED`)

Every entrypoint publishes the container identity for shells with
`write-bridge-env.sh`. If that write fails the box still STARTS (an unreachable
container cannot be debugged) but leaves `/etc/matrx/bridge-env.FAILED` naming
the failure; `bridge-headers.sh` and `matrx_agent.bridge_headers` read it and
refuse loudly instead of taking the quiet "unwired image" path. If a user reports
`git push` or `mtx` failing with "not configured", read that file first:

```bash
docker exec <sbx-id> cat /etc/matrx/bridge-env.FAILED     # absent = the writer succeeded
docker exec <sbx-id> /opt/sandbox/scripts/write-bridge-env.sh   # re-run it; success clears the marker
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
3. Inventory exact containers, mounts, image consumers, rollback tags and migration journals. Stopped containers may contain the only copy of a user's files.
4. Reclaim only individually verified unreferenced artifacts; never use broad system/container/volume pruning or delete migration locks/journals to make deployment pass.
5. Confirm free space and let the existing deployment owner resume the approved release path.

The deploy pipeline prunes dangling images before each pull and removes pulled candidate aliases after a verified release. If disk pressure persists, investigate the SSM output and ECR/Docker retention instead of deleting live or rollback tags.

Since 2026‑08‑11 `scripts/deploy-ec2.sh` also **reclaims leaked per‑SHA candidates** (`<ecr>:<sha>`, `:slim-<sha>`, `-orchestrator:<sha>` from earlier releases) before pulling, deletes them on the failure path too, and **refuses to start** with `[deploy-ec2] ERROR: only N MiB free …` plus `df`/`docker system df` output when under 10 GiB. That replaced the old failure mode: a failed release leaked ~4 GB of tags, and the next deploys died mid‑pull with the unreadable `register layer` error — which is exactly how EC2 fell three releases behind on 2026‑08‑09 and 2026‑08‑11. A disk‑full deploy now names the disk in the first error line.

### 2. Orchestrator code on EC2 is stale even though deploy "succeeded"

Symptom: authenticated `/api-surface` reports a `source_sha` other than the approved release, or returns 404.

Root cause (legacy): pre-v0.2.0 deploy.yml only built/pushed Docker images and called `systemctl restart`. But the systemd unit runs Python from `/home/ec2-user/orchestrator/` — not from any Docker container. So `restart` reloaded the same old on-disk code over and over.

Recovery: re-run `deploy.yml` for `main`. Do not hand-copy files or install an editable package into the live directory; that bypasses the locked candidate, migration gate, rollback capture, and exact contract assertion in `scripts/deploy-ec2.sh`. If the workflow itself is broken, repair and re-run the pipeline while the last-known-good service remains live.

**Second root cause, found 2026‑08‑11 — the release could not boot, and said it did.** Two bugs stacked, and together they pinned EC2 on `f229d4b` for weeks while every deploy record read green:

- **Never let anything in the release depend on the candidate path.** `uv sync` builds the venv in `orchestrator-candidate-<sha>/`, and promotion **moves** that directory to `/home/ec2-user/orchestrator`. uv pins console-script shebangs to the absolute build path, so `.venv/bin/uvicorn` is unrunnable the instant it goes live — `systemd[1]: Failed to execute .../.venv/bin/uvicorn: No such file or directory`, status `203/EXEC`, crashloop, contract assertion times out, rollback. The systemd drop-in therefore execs **`.venv/bin/python -m uvicorn`** (that `python` is a symlink to `/usr/bin/python3.11` and survives the move), and the script refuses to promote a candidate that cannot run it. If you add any other entry point, re-check it against a *moved* venv, not the built one.
- **A rolled-back release must exit non-zero.** `rollback()` starts with `trap - ERR` + `set +e`; returning from it resumed the script after the failing statement with `-e` disabled, so it ran the success epilogue and logged `release … is healthy and exact` with exit 0. GitHub showed a successful deploy over a live old revision. `rollback()` now exits 1. Related trap: **`exit` does not fire the ERR trap** — a failure path that calls `fail()` after the rollback trap is installed will skip the rollback and leave the broken release live. Call `rollback` directly there.

The contract assertion also dumps `systemctl status`, the unit journal, and the last `/api-surface` payload before rolling back. It used to be a bare `|| false`, which is why the boot failure was invisible in the workflow log.

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
curl -H "X-API-Key: $EC2_KEY" https://sandbox-orchestrator.matrxserver.com/system | jq

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
| Development sandbox token mint returns 500/502 while connection preparation is failing | The pre-token SessionStart hook failed | Current orchestrators log the hook exception, return `connection_hooks.status=failed`, and still issue the scoped token; redeploy if the endpoint still fails closed. The next binding retries preparation. |
| `/extend` returns 200 but `expires_at` doesn't change | Pre-v0.2.0 orchestrator (stub still in place) | Redeploy with the latest image |
| Traefik 404 on `orchestrator.dev.codematrx.com` | DNS not resolving or Traefik labels missing | `dig orchestrator.dev.codematrx.com` (must point at `77.37.62.64`); `docker inspect matrx-orchestrator \| grep traefik` |

## Passing the full aidream env into spawned sandboxes (added 2026-04-28)

The hosted orchestrator's `docker-compose.yml` (at `/srv/apps/sandbox-orchestrator/docker-compose.yml`) now loads TWO env files:

1. `.env` — the orchestrator's own settings (`MATRX_API_KEY`, `MATRX_PUBLIC_URL`, `MATRX_ACCESS_TOKEN_SECRET`, etc.)
2. `/srv/projects/aidream/.env` — the full aidream prod env (~159 vars) — Supabase URL/keys/JWT secret, AI provider API keys, admin tokens, etc.

The orchestrator never stores values from #2 as typed settings. It reads from `os.environ` at sandbox-create time and forwards every name listed in `MATRX_AIDREAM_PASSTHROUGH_ENV` (default covers Supabase + JWT secret + ~20 AI provider keys + admin identifiers) to the spawned container's environment. This is what makes aidream's FastAPI inside `:aidream` template sandboxes able to validate user JWTs and call AI providers.

🚨 **Isolation (incident 2026-09-13, [incidents/2026-09-13-platform-env-leak.md](incidents/2026-09-13-platform-env-leak.md)):** the passthrough reaches ONLY the `aidream` template — every other template gets nothing from the orchestrator's environment — and even for `aidream`, names that look like master credentials (`*PASSWORD*`, `*_SECRET*`, `*DATABASE_URL*`, `*_SERVICE_TOKEN`, `ADMIN_*TOKEN*`, `*_API_KEY`, …) are withheld unless the knob `infrastructure.sandbox.aidream_template_forwards_master_credentials` is on (default OFF; a missing row is OFF). So with the knob off an `aidream` box validates JWTs only if the JWT secret is provided another way — turn the knob on for a short supervised dev session and rotate afterwards. `GET /sandboxes/{id}/diagnostics` → `platform_env_leaked_count` shows a box still carrying names it should not have (born before the fix): migrate or recreate it.

Verify with:
```bash
curl -s https://orchestrator.dev.codematrx.com/ \
  | python3 -c "import json,sys; ap=json.load(sys.stdin)['integrations']['aidream_passthrough']; \
print(f'set ({ap[\"configured_count\"]}): {ap[\"configured_keys\"]}')"
```

Should show 25+ keys with `SUPABASE_MATRIX_JWT_SECRET` among them. If it doesn't, `aidream/.env` isn't being read — check the `env_file` block in the compose file.

---

## Out-of-repo live settings — replicate these (added 2026-05-23)

Everything below is **live state that is NOT captured by the matrx-sandbox repo**. If this server were rebuilt from the repos alone, these would be missing. Recorded here so they're replicable.

### 1. Warm pool — RETIRED (2026-09-17)

There is no warm pool. A box pre-booted before anyone knows whose it is carries
a sentinel user and no organization, and Docker cannot change a running
container's environment, so a claim could never give it the real identity every
AI Dream call must carry — while the claim path had in fact been unreachable
since `organization_id` became required, so the loop booted and retired
containers for nobody. `POST /sandboxes/claim` still answers and cold-creates.
On boot the orchestrator sweeps leftover UNCLAIMED warm containers away and
logs a WARNING if the `warm_pool_size` / `warm_pool_templates` settings still
ask for boxes — set them to 0/empty. Reasoning: `orchestrator/pool.py`.

The historical note below is kept because the SETTINGS rows still exist.

### 1a. Warm-pool config — a SETTING since 2026-09-11 (now inert)

Historically appended to `/srv/apps/sandbox-orchestrator/.env` as
`MATRX_WARM_POOL_SIZE=2` / `MATRX_WARM_POOL_TEMPLATE=slim` (and mirrored in the
EC2 systemd unit). Since 2026-09-11 (USD-5, "Never an env var") the warm pool —
with every other fleet-shape value — is a `platform.feature_knob` row under
`infrastructure.sandbox` (`warm_pool_size` = 2, `warm_pool_template` = slim,
seeded by aidream migration 0636 at the values both tiers were running). Both
orchestrators read the same rows through `orchestrator/knobs.py`, so this is
no longer "live state not captured by the repo": rebuild a host and it inherits
the setting. The env lines still present in the hosted `.env` and the EC2 unit
are inert and can be deleted at the next touch. Since the retirement above,
the SETTING is inert too — nothing reads it to warm anything. Code:
`orchestrator/pool.py` (retirement sweep), wired in `orchestrator/main.py`
lifespan.

### 2. `user_memory` migration applied to Supabase

Migration [`migrations/004_user_memory.sql`](../orchestrator/migrations/004_user_memory.sql) is present in the live Matrx Main East Supabase project (`brsgrqvjdzwihsvnfqkf`). The former West project (`txzxabzwovsujtloxrus`) is rollback-only and must not receive sandbox writes or scheduled work. New table `user_memory` is additive and backs the per-user cross-project memory (see [MEMORY_API.md](MEMORY_API.md)).

### 3. `matrx-sandbox:slim` image build

The lightweight coding box is built from [`sandbox-image/Dockerfile.slim`](../sandbox-image/Dockerfile.slim) (in the repo). To (re)build locally on this host:

```bash
cd /srv/projects/matrx-sandbox/sandbox-image && docker build -f Dockerfile.slim -t matrx-sandbox:slim .
```

On EC2 the CI builds + pushes immutable `:slim-<commit-sha>` candidates to ECR and the SSM deploy pulls the approved revision and tags it locally (see `.github/workflows/deploy.yml`). The hosted warm pool spawns from this local `matrx-sandbox:slim`.

### 4. New orchestrator capabilities (all in-repo, no separate action)

These are committed/pushed in matrx-sandbox `main`; listed for awareness — the running container picks them up on the next rebuild (already done on hosted):
- Expiry reaper (`reaper.py`) + `POST /sandboxes/{id}/resume`.
- Warm pool (`pool.py`) + `POST /sandboxes/claim`.
- Per-user memory (`memory_sync.py`, store methods, `/users/{id}/memory`).
- Scoped-token acceptance on the structured tool routes (`middleware/auth.py`) + `POST /sandboxes/{id}/agent-binding` (the conversation-handoff primitive).

See [CONVERSATION_HANDOFF.md](CONVERSATION_HANDOFF.md) for the production low-latency plan (co-located AI Dream) that consumes these.
