# Zero-Drift Sandbox Migration

> **Historical design and test evidence — not a current safety guarantee.**
> The September 8 audit invalidated the blanket persistence, atomicity and
> failure-safety claims below. EC2 does not imply S3-backed home storage; retained
> volumes alone do not prove unchanged files. The current safety contract and
> independent acceptance gates are in the canonical
> [sandbox register](../../common-docs/systems/infrastructure/sandboxes/REGISTER.md).
> Keep both migration gates disabled. Do not use this document to authorize a
> user-container replacement or to re-enable automatic migration.

Cross-repo system-of-record: /Users/armanisadeghi/code/common-docs/systems/infrastructure/sandboxes/STATE.md — read it before touching this feature in ANY repo.

---

## The mental model in one paragraph

A sandbox's **data lives outside its container** — in a per-user Docker volume mounted at `/home/agent` (hosted tier) or in S3 (ec2 tier). The container is disposable. So "migrate a box to a new image" is: **build a new container on the new image, mount the SAME volume, keep the SAME logical `sandbox_id`, verify it's healthy, then atomically swap the old container out.** Because the `sandbox_id` (and the agent's HMAC access token, which is bound to it) never change, the agent's existing binding keeps working across the swap. The replacement starts behind a migration hold. Before commit it exposes health only. A read-only preflight then proves the agent can update its declared lifecycle-owned paths (`.matrx/session-report.md`, session manifests/locks, runtime queues, and configured `cloud-files`) before routing changes. After commit it activates runtime services without normal home bootstrap, layout regeneration, credential rewrites, cloud down-sync, aidream checkout seeding, or ownership initialization. Pre-existing user/content paths—including `.matrx/instructions`—retain bytes, modes, owners, and group owners; lifecycle-owned files continue their documented updates. The commit gate lives in an exact labelled Docker volume outside both user storage and the hosted aidream tmpfs set, so a container restart cannot return a committed sandbox to hold.

---

## The three layers

### 1. Version stamping — *what version is this?*

Every image carries a version baked at build time (`MATRX_IMAGE_VERSION`), exposed three ways:
- an **ENV** var (visible in `docker inspect` and inside the box),
- a **LABEL** `com.aimatrx.sandbox.version` (inspectable without running),
- a file **`/etc/sandbox-image-version`** (the box reads it to self-verify on a migration boot).

The `aidream` variant additionally carries the exact 40-character aidream source
commit in `com.aimatrx.aidream.sha` and root-owned
`/etc/aidream-image-sha`. Its persistent working copy must match that commit and
have no tracked modifications before managed autostart or diagnostics can call
it release-ready. This separate source gate matters because `/home/agent` data
survives image migration.

**Always build through [`sandbox-image/build.sh`](../sandbox-image/build.sh)** — it stamps the version (git short-sha + `-dirty` if the tree is dirty + UTC timestamp). A bare `docker build` leaves the version as `"dev"`, which the drift report flags as unversioned.

```bash
./sandbox-image/build.sh slim        # build matrx-sandbox:slim, stamped
./sandbox-image/build.sh core        # build :core
./sandbox-image/build.sh aidream     # build :core then :aidream (inherits core's stamp)
./sandbox-image/build.sh all
MATRX_IMAGE_VERSION=v1.2.3 ./sandbox-image/build.sh slim   # pin an explicit version
```

### 2. Drift detection — *which boxes are stale?*

`orchestrator/versioning.py` compares each **live, claimed** box to the **current** image for its template. Detection is by **image ID** (exact, and works on boxes already running — no rebuild needed); the baked version string is shown for humans + used in the migration self-check. Warm/unclaimed pool boxes are skipped (the warm-pool refresher handles those).

- **`GET /drift`** (master-key) — tier-scoped report: `{tier, total, drifted, stale_sandbox_ids, boxes:[…]}`. Each box row carries `running_image_id`, `running_version`, `current_image_id`, `current_version`, `drifted`, `reason`.
- The **reaper** logs a loud `SANDBOX VERSION DRIFT` warning every sweep when any box is stale, and includes a `drifted=N` count in its periodic line — so drift is never silent.

Detection is **tier-scoped by construction**: each orchestrator only sees its own host's containers, so the ec2 orchestrator reports ec2 drift and the hosted one reports hosted drift. Run `/drift` against each.

### 3. Migration — *move a box to the current image, safely*

`orchestrator/migrate.py::migrate_sandbox(sandbox_id)`:

1. **Mark migrating** (whole window). From here every new tool call to the box is refused with a **retryable `503`** (`{"detail":{"status":"migrating"}}` + `Retry-After`). The agent's tool proxy waits it out and lands on the new container — no `404`, no error.
2. **Build** the new container on the current image, mounting the **same volume**, copying the old container's env/labels (minus the stale `MATRX_IMAGE_VERSION`), with `SANDBOX_MIGRATION=1` so the entrypoint skips the cloud-files down-sync (data's already on the volume — this is the biggest time saver).
3. **Verify** readiness (`/tmp/.sandbox_ready`) **and** that the box reports the expected baked version (`/etc/sandbox-image-version`).
4. **Drain** any calls that were in-flight when we locked (never cut over mid-tool-execution).
5. **Atomic cutover**: stop + rename old → `<id>-old-<ts>`, rename new → `<id>`, remove old.
6. **Release** the lock — the new container now answers as `sandbox_id`; calls resume.

**Failure is safe at every step.** If the new box doesn't come up healthy/correct, it's removed and the **old box keeps running untouched** (loud `MIGRATE FAILED` alarm). If cutover itself fails, it rolls back to the old container. Data is in the volume throughout, so nothing is ever at risk.

Endpoints:
- **`POST /sandboxes/{id}/migrate`** — migrate one box (master-key). The default remains idle-only. An owner-facing caller that has shown an interruption warning may send `?interrupt_attached_sessions=true`; this permits idle PTY/watch attachments, but executing tool calls are fenced and must drain. `?target_image=` accepts only an immutable image identity. The caller supplies or receives a canonical `operation_id`; success retains the prior `status`/version fields and adds an exact terminal projection, busy refusal is a structured `409`, and failure is `502` with the old box intact. A disconnected caller reconnects with `GET /sandboxes/{id}/migration?operation_id=...`; this read never starts or retries a migration, exposes no journal payload, and reports an unowned nonterminal operation as `recovery_required` rather than guessing success. Exact no-op results have a durable terminal receipt; the S3-ordered path remains refused until it has the same crash-safe operation journal.
- **`POST /migrate-all`** — roll every drifted box on this tier (the manual trigger for the rolling migration).

---

## The safety contract (no data loss, no confusion, minimal delay)

| Concern | How it's guaranteed |
|---|---|
| **No data loss** | The per-user volume / S3 is never touched by the swap. The new container mounts the same volume. Verified live: writes made before *and* during a migration are all present afterward (49/49 acked writes). Migration verifies the new box *before* cutover and keeps the old box on any failure. |
| **No agent confusion** | The box is "migrating" for the whole swap; calls get a retryable `503` and the matrx-ai tool proxy ([`_sandbox_proxy.py`](../../aidream/packages/matrx-ai/matrx_ai/tools/_sandbox_proxy.py)) retries (Retry-After-paced, long enough to outlast a full migration). Same `sandbox_id` + token survive the swap, so the retried call just lands on the new container. No `404`, no hard error. |
| **Never interrupt a running tool** | In-flight calls are fenced and drained before cutover. Every request reserves an exact process-local operation token *before* taking its durable shared home lock; migration admission uses the same fence, then waits for every pre-fence token and shared lock to exit before taking the exclusive lock. Automatic and unconfirmed paths additionally require no PTY/watch attachment or recent activity. A confirmed owner update signals and closes idle attachments, including a WebSocket still resolving when the fence lands, but never interrupts an executing tool call. The same admission contract wraps hosted named-volume migration and the gated EC2/S3 path. |
| **Held startup is home-inert** | Every production entrypoint variant, including the hosted/local image, starts only the health API while `MATRX_MIGRATION_HOLD=1`; layout, credential, terminal, and cloud-sync writes wait for the durable commit marker. If an image nevertheless changes the home before CAS, migration refuses commit. Recovery validates the immutable backup before mounting the source writable, restores the proven pre-migration manifest, removes the exact held target, and resumes the original paused process. A missing or corrupt backup remains fenced for investigation. |
| **Rollback restores durable network identity** | Recovery reconnects the same immutable Docker network and requested aliases. Explicit endpoint IPAM addresses remain exact; Docker auto-IPAM addresses and generated MACs may be reassigned on reconnect and are diagnostic observations, not durable routing identity. Proxies resolve the current container address from Docker on each request. |
| **Shared homes stay shared safely** | Hosted homes are user-scoped, so more than one sandbox can mount the same volume. An individual image update never pauses or replaces an unconfirmed sibling. Before writing durable migration intent it inventories actual Docker writers; a live sibling returns structured `busy_deferred` with the action to stop the other sandbox and a guarantee that nothing changed. The same condition encountered during recovery remains a hard fence because restoring there could overwrite sibling writes. |
| **Deployments cannot interrupt migration** | Hosted and EC2 migrations/recovery own the shared side of one durable `deployment.lock` through terminal cleanup; release promotion owns the exclusive side through exact candidate source and health. The first upgrade from a lockless control plane freezes only that control plane, then a descriptor-safe helper running as the actual service identity creates journal-derived locks and exclusively acquires the complete existing lock inode set without following links, changing ownership/mode, or truncating bytes. It rechecks exact name/device/inode identities before the deployer audits durable records and artifacts and fatally replaces the exact old process. Hosted uses paused-container `KILL` (never a TERM-based compose stop); EC2 uses cgroup-v2 freeze plus `cgroup.kill` with runtime restart disabled. Pending, corrupt, unreadable, cleanup-incomplete, contended, replaced, malformed, or orphaned state defers without retagging or replacing anything. A retained helper-image pin remains acceptable only through its exact cleanup receipt; that receipt is carried durably when a later operation replaces the sandbox journal. Terminal cleanup-complete recovery is a true no-op before Docker/store access, so startup cannot deadlock release verification or repeat cleanup. |
| **Minimal delay** | Idle boxes (the only ones the auto-path migrates) migrate with zero agent-visible impact. The `SANDBOX_MIGRATION=1` cloud-sync skip cuts migration time substantially. |

The in-flight accounting + migrating lock live in `orchestrator/activity.py` (the orchestrator proxies every tool call, so it knows exactly when a box is idle vs busy — no guessing, no cross-service polling).

---

## Automation

**S3 migration remains unverified; keep `MATRX_ENABLE_S3_MIGRATE` disabled.**
No-volume non-core templates explicitly refuse migration even if that gate is
enabled. Slim uses git persistence; neither its uncommitted home nor an EC2
tier label proves S3 durability. Existing user boxes require a mount/data
census and independently verified preservation before any image replacement.

The snapshot experiment in commit `7865f91` was reverted after independent
review found missing exclusive migration admission, shared-user-prefix write
isolation, verified rollback readiness, durable crash recovery, and protection
from expiry/zombie cleanup. It is historical design evidence, not a production
migration path. Future work must resolve those hazards and verify the real
Docker tar round-trip plus user file bytes/modes after readiness; mocked archive
tests cannot authorize a user-fleet rollout.

`migrate_all_drifted()` rolls drifted boxes one at a time (busy ones return `busy_deferred` and retry on the next pass — the "keep checking until it's idle, then migrate" loop). It's wired into the reaper, gated behind the `infrastructure.sandbox.auto_migrate` setting (a `platform.feature_knob` row since 2026-09-11 — it was the env var `MATRX_AUTO_MIGRATE`; USD-5, never an env var):

- `auto_migrate` on — each reaper sweep (every 60s) migrates up to the `migrate_max_per_pass` setting (2 today) drifted, **idle** boxes; busy ones defer to the next sweep.
- With it OFF, nothing migrates automatically; `POST /migrate-all` and per-box `/migrate` still work for manual/triggered rollout.

**Current hold (2026-09-08, re-verified 2026-09-11 as the seeded setting values):** `auto_migrate` is false on
both tiers; `enable_s3_migrate` must remain off. The earlier May
rollout is not current authorization. Source `c5794ea` removes migration from
EC2 deployment, and `766d707` removes migration from development connection
preparation while preserving repository synchronization. Deployment and live
containment evidence belongs only in the canonical register linked above.

Event-loop responsiveness does not prove durable storage, exclusive admission,
rollback safety or crash recovery. Explicit migration endpoints remain exposed,
but their existence is not permission to run them against user work before the
register's independent preservation gates pass.

### From the Server Manager UI

The Manager's **orchestrator-sandboxes** admin page (`manager.dev.codematrx.com`) shows a **"Version drift" card** whenever any box is stale: it lists each drifted box (`running → current` version) and a **"Migrate all"** button. Backed by Manager proxy routes (`/api/orchestrator-sandboxes-drift`, `-migrate-all`, `/:id/migrate`) that call the orchestrator with the master key. So operators get drift visibility + one-click migration without the CLI or the API key.

---

## Known limitations

1. ~~Blocking docker calls freeze the orchestrator event loop.~~ **FIXED (commit `e3e1770`, both tiers).** All blocking `docker-py` calls (`exec_run`, `containers.run`, stop/rename/remove, readiness poll, drift scan, reconcile, warm pool, memory sync) are now wrapped in `asyncio.to_thread`, so a long exec or a migration build no longer freezes the orchestrator. Proven under load: during a 41s migration the orchestrator stayed responsive and served 32 retryable 503s cleanly. **Any NEW blocking docker call added in an `async def` MUST be `to_thread`-wrapped** — that's the standing rule now.
2. **Migration time** is dominated by new-container readiness; the `SANDBOX_MIGRATION=1` cloud-sync skip cut it to ~40s. Only matters if a call arrives mid-migration (it transparently retries via 503); idle boxes (the only ones auto-migrated) migrate invisibly.
3. **Version labels** on hosted `:core`/`:aidream` images populate on their next `build.sh` rebuild; until then they read as unversioned (`dev`). Drift detection works by **image ID** regardless, so auto-migration is fully functional for those templates either way — the label is cosmetic. (ec2 images are versioned by CI; hosted `:slim` is versioned.)

---

## File map

| File | Role |
|---|---|
| [`sandbox-image/Dockerfile`](../sandbox-image/Dockerfile) · [`Dockerfile.slim`](../sandbox-image/Dockerfile.slim) | Bake `MATRX_IMAGE_VERSION` (ENV + LABEL + `/etc/sandbox-image-version`) |
| [`sandbox-image/build.sh`](../sandbox-image/build.sh) | The single stamping build entrypoint |
| [`sandbox-image/scripts/entrypoint*.sh`](../sandbox-image/scripts/) | Skip cloud-sync when `SANDBOX_MIGRATION=1` |
| [`orchestrator/versioning.py`](../orchestrator/orchestrator/versioning.py) | Version read + drift detection |
| [`orchestrator/migrate.py`](../orchestrator/orchestrator/migrate.py) | `migrate_sandbox` + `migrate_all_drifted` |
| [`orchestrator/activity.py`](../orchestrator/orchestrator/activity.py) | In-flight tracking + migrating lock |
| [`orchestrator/release_barrier.py`](../orchestrator/orchestrator/release_barrier.py) · [`release_lock_holder.py`](../orchestrator/orchestrator/release_lock_holder.py) | Fail-closed release artifact audit plus descriptor-safe service-identity lock lease |
| [`orchestrator/main.py`](../orchestrator/orchestrator/main.py) | `GET /drift`, `POST /migrate-all` |
| [`orchestrator/routes/sandboxes.py`](../orchestrator/orchestrator/routes/sandboxes.py) | `POST /{id}/migrate`; the 503-migrating guard on exec/fs/git |
| [`orchestrator/connection_hooks.py`](../orchestrator/orchestrator/connection_hooks.py) | Development-repository synchronization; no implicit image migration |
| [`orchestrator/reaper.py`](../orchestrator/orchestrator/reaper.py) | Drift alarm + opt-in auto-migrate |
| [`scripts/deploy-ec2.sh`](../scripts/deploy-ec2.sh) | Orchestrator/image deployment; no implicit user-container migration |
| aidream `matrx-ai/.../tools/_sandbox_proxy.py` | Agent-side transparent retry on 503-migrating |

---

## Config reference

| Env var | Default | Meaning |
|---|---|---|
| `MATRX_IMAGE_VERSION` | `dev` (build ARG) | Baked image version. Set by `build.sh`. |
| ~~`MATRX_AUTO_MIGRATE`~~ → setting `infrastructure.sandbox.auto_migrate` | off | Reaper auto-migrates drifted idle boxes each sweep. Not an env var since 2026-09-11. |
| ~~`MATRX_MIGRATE_MAX_PER_PASS`~~ → setting `infrastructure.sandbox.migrate_max_per_pass` | `2` | Max boxes migrated per reaper sweep (rolling cap). |
| `SANDBOX_MIGRATION` | unset | Set to `1` by the migrator on the new container; entrypoint skips cloud-sync. Not for manual use. |
