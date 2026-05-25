# Zero-Drift Sandbox Migration

> **Requirement (the whole point):** when a new sandbox image is published, **every** running box must end up on it — automatically — with **no data loss, no agent confusion, and minimal delay**. Whether there are two boxes or two million, none may run stale code. This document is the authoritative description of how that works.

---

## The mental model in one paragraph

A sandbox's **data lives outside its container** — in a per-user Docker volume mounted at `/home/agent` (hosted tier) or in S3 (ec2 tier). The container is disposable. So "migrate a box to a new image" is: **build a new container on the new image, mount the SAME volume, keep the SAME logical `sandbox_id`, verify it's healthy, then atomically swap the old container out.** Because the `sandbox_id` (and the agent's HMAC access token, which is bound to it) never change, the agent's existing binding keeps working across the swap. The only thing that changes is which container answers — and the data was never touched.

---

## The three layers

### 1. Version stamping — *what version is this?*

Every image carries a version baked at build time (`MATRX_IMAGE_VERSION`), exposed three ways:
- an **ENV** var (visible in `docker inspect` and inside the box),
- a **LABEL** `com.aimatrx.sandbox.version` (inspectable without running),
- a file **`/etc/sandbox-image-version`** (the box reads it to self-verify on a migration boot).

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
- **`POST /sandboxes/{id}/migrate`** — migrate one box (master-key). `?target_image=` to force a specific image. `502` (with the old box intact) on failure; `200` with `{status: migrated|already_current|busy_deferred}`.
- **`POST /migrate-all`** — roll every drifted box on this tier (the manual trigger for the rolling migration).

---

## The safety contract (no data loss, no confusion, minimal delay)

| Concern | How it's guaranteed |
|---|---|
| **No data loss** | The per-user volume / S3 is never touched by the swap. The new container mounts the same volume. Verified live: writes made before *and* during a migration are all present afterward (49/49 acked writes). Migration verifies the new box *before* cutover and keeps the old box on any failure. |
| **No agent confusion** | The box is "migrating" for the whole swap; calls get a retryable `503` and the matrx-ai tool proxy ([`_sandbox_proxy.py`](../../aidream/packages/matrx-ai/matrx_ai/tools/_sandbox_proxy.py)) retries (Retry-After-paced, long enough to outlast a full migration). Same `sandbox_id` + token survive the swap, so the retried call just lands on the new container. No `404`, no hard error. |
| **Never interrupt a running tool** | In-flight calls are *drained* before cutover. The auto-path additionally only migrates boxes with **zero in-flight calls** (`require_idle=True`). |
| **Minimal delay** | Idle boxes (the only ones the auto-path migrates) migrate with zero agent-visible impact. The `SANDBOX_MIGRATION=1` cloud-sync skip cuts migration time substantially. |

The in-flight accounting + migrating lock live in `orchestrator/activity.py` (the orchestrator proxies every tool call, so it knows exactly when a box is idle vs busy — no guessing, no cross-service polling).

---

## Automation

`migrate_all_drifted()` rolls drifted boxes one at a time (busy ones return `busy_deferred` and retry on the next pass — the "keep checking until it's idle, then migrate" loop). It's wired into the reaper, **gated behind `MATRX_AUTO_MIGRATE=1` (default OFF)**:

- `MATRX_AUTO_MIGRATE=1` — each reaper sweep migrates up to `MATRX_MIGRATE_MAX_PER_PASS` (default 2) drifted, idle boxes; busy ones defer to the next sweep.
- With it OFF, nothing migrates automatically; `POST /migrate-all` and per-box `/migrate` still work for manual/triggered rollout.

**Recommended rollout:** keep `AUTO_MIGRATE` off until the [known limitation](#known-limitations) below is addressed, then enable hosted-first, watch a day, then ec2.

---

## Known limitations

1. **Blocking docker calls freeze the orchestrator event loop (pre-existing, being fixed separately).** The orchestrator makes synchronous `docker-py` calls (`exec_run`, `containers.run`, stop/rename) inside async handlers, so a long exec *or* a migration's container build briefly starves all other calls on that orchestrator. This is an orchestrator-wide issue, not specific to migration — the fix is to wrap blocking docker calls in `asyncio.to_thread`. **Migrations are data-safe regardless;** this is an availability/throughput concern and the reason `AUTO_MIGRATE` should stay off until it lands (a migration shouldn't degrade sibling boxes at scale).
2. **Migration time** is dominated by new-container readiness; the `SANDBOX_MIGRATION=1` cloud-sync skip helps, but a heavy image still takes tens of seconds to come up. Only matters if a call arrives mid-migration (it retries); idle boxes migrate invisibly.

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
| [`orchestrator/main.py`](../orchestrator/orchestrator/main.py) | `GET /drift`, `POST /migrate-all` |
| [`orchestrator/routes/sandboxes.py`](../orchestrator/orchestrator/routes/sandboxes.py) | `POST /{id}/migrate`; the 503-migrating guard on exec/fs/git |
| [`orchestrator/reaper.py`](../orchestrator/orchestrator/reaper.py) | Drift alarm + opt-in auto-migrate |
| aidream `matrx-ai/.../tools/_sandbox_proxy.py` | Agent-side transparent retry on 503-migrating |

---

## Config reference

| Env var | Default | Meaning |
|---|---|---|
| `MATRX_IMAGE_VERSION` | `dev` (build ARG) | Baked image version. Set by `build.sh`. |
| `MATRX_AUTO_MIGRATE` | `0` (off) | Reaper auto-migrates drifted idle boxes each sweep. |
| `MATRX_MIGRATE_MAX_PER_PASS` | `2` | Max boxes migrated per reaper sweep (rolling cap). |
| `SANDBOX_MIGRATION` | unset | Set to `1` by the migrator on the new container; entrypoint skips cloud-sync. Not for manual use. |
