# Server-Agent Handoff — Push-to-Main Auto-Rebuild & Roll

**Audience:** the agent operating **directly on the servers** (the `/srv` hosted
host and the EC2 host). **Author:** the repo-side agent that landed the
`claude/modest-babbage-htdoey` branch.

**Why this exists:** the bulk of the audit fixes and the new
rebuild-and-roll machinery are now in the repo, but several steps can only be
done — and validated — **on the live machines**: applying DB migrations,
setting orchestrator environment variables, rebuilding the heavy `aidream`
image, validating two data-loss-sensitive paths before enabling them, and
confirming the end-to-end loop. This document is your work order.

> **Prime directive (the end state you must reach and prove):**
> **Every push to `main` rebuilds ALL sandbox templates, and existing user
> sandboxes are then rolled onto the freshly-built image — but a sandbox that is
> currently in use is never interrupted; it is scheduled to refresh as soon as
> it goes idle.** You are done only when you have *demonstrated* this end to end
> on both tiers and every box in the [Victory Checklist](#victory-checklist)
> passes. **Loop on the checklist until all items are green.**

---

## 0. What the repo already gives you (do not rebuild these)

These shipped on this branch / earlier — your job is to **configure, wire, and
validate**, not to reimplement:

| Capability | Where | State |
|---|---|---|
| Hosted deploy: rebuild orchestrator + `core`/`slim`/`aidream`, health-gated, rollback, self-heal | `scripts/deploy-hosted.sh`, `.github/workflows/deploy.yml` (`deploy-hosted` job) | **Working** |
| EC2 deploy: build/push `core`+`slim`+orchestrator to ECR, SSM deploy, health loop | `deploy.yml` (`deploy` job) | **Working** |
| Rolling auto-migrate (drift → migrate idle boxes, defer busy) | `orchestrator/reaper.py` + `orchestrator/migrate.py`, gated by `MATRX_AUTO_MIGRATE` | **Working, OFF by default** |
| Per-box / bulk migrate endpoints, drift report | `POST /sandboxes/{id}/migrate`, `POST /migrate-all`, `GET /drift` | **Working** |
| Idle gate: defer migrate on in-flight calls **or** recent heartbeat | `migrate.py` (`MATRX_MIGRATE_RECENT_HEARTBEAT_SECONDS`, default 120s) | **New, on** |
| EC2/S3-ordered in-place migrate (flush→stop→boot-fresh→cutover) | `migrate._migrate_s3_ordered`, gated by `MATRX_ENABLE_S3_MIGRATE` | **New, OFF — must validate before enabling** |
| Per-sandbox daemon shared-secret (cross-sandbox isolation) | daemon `matrx_agent/api/_auth.py` + orchestrator `agent_token_for()`; active when `MATRX_ACCESS_TOKEN_SECRET` is set **and** the image is rebuilt | **New, fail-open — must rebuild image + validate** |
| Idempotent DB migration runner | `python -m orchestrator.migrate_runner [--dry-run]` | **New — must wire into deploys** |

**New env vars introduced on this branch** (all optional, safe defaults):
`MATRX_MIGRATE_RECENT_HEARTBEAT_SECONDS` (120), `MATRX_ENABLE_S3_MIGRATE`
(false). Existing but **must be set** for the goal: `MATRX_AUTO_MIGRATE`,
`MATRX_ACCESS_TOKEN_SECRET`, and (EC2) `MATRX_AIDREAM_SERVICE_TOKEN`
(FOUND_DEFECTS Bug 7).

---

## 1. Mental model of "rebuild → roll"

1. A push to `main` triggers the deploy jobs. They **rebuild the template
   images** (`matrx-sandbox:core`, `:slim`, `:aidream`, and — see Task C — the
   `local` starter image), each stamped with the commit SHA as its zero-drift
   version.
2. After a rebuild, every **running** sandbox is on the **old** image id, so
   `GET /drift` reports it drifted.
3. With `MATRX_AUTO_MIGRATE=1`, the reaper sweeps every 60s and calls
   `migrate_all_drifted`. For each drifted box it runs `migrate_sandbox(...,
   require_idle=True)`:
   - **In use?** (an in-flight tool call **or** a heartbeat newer than
     `MATRX_MIGRATE_RECENT_HEARTBEAT_SECONDS`) → returns `busy_deferred`; retried
     next sweep. **This is the "protect in-use, refresh when idle" guarantee.**
   - **Idle?** → builds a new container on the current image, **keeping the same
     `sandbox_id` and the user's data**, verifies readiness + version, cuts over
     atomically, removes the old container.
4. Hosted-tier boxes migrate in place via the shared `/home/agent` volume.
   EC2/S3 boxes migrate via the **S3-ordered** path (Task E) — **only once you
   enable `MATRX_ENABLE_S3_MIGRATE` after validating it.**

You do **not** need to write migration logic. You need to **turn it on, feed it
freshly-built images, and prove the guarantees hold.**

---

## 2. Tasks

Work top-to-bottom. Each task ends with a **Verify** you must pass before moving
on. Re-run verifies after any change (this is the loop).

### Task A — Wire the DB migration runner into every deploy

The store reads columns (`deleted_at`) that only exist if migrations are
applied. Migrations were previously hand-run. Make them automatic and ordered.

1. **Hosted** (`scripts/deploy-hosted.sh`): before the orchestrator container is
   recreated/health-checked, run the runner against the hosted DB. Add a step
   that executes inside the orchestrator image (it has asyncpg + the code), e.g.:
   ```bash
   docker run --rm --env-file /srv/apps/sandbox-orchestrator/.env \
     matrx-orchestrator:latest python -m orchestrator.migrate_runner
   ```
   Place it **after** the image build and **before** `docker compose up -d`.
   Fail the deploy if it exits non-zero.
2. **EC2** (`deploy.yml` `deploy` job, SSM command block): after step `[4/5]`
   (pip install) and **before** `systemctl restart`, add:
   ```bash
   sudo -u ec2-user bash -c "cd /home/ec2-user/orchestrator && \
     MATRX_DATABASE_URL=$MATRX_DATABASE_URL /usr/bin/python3.11 -m orchestrator.migrate_runner"
   ```
   (Pull `MATRX_DATABASE_URL` from the systemd env / SSM Parameter Store — do not
   hardcode it.)
3. **Apply once, now**, by hand, so the current schema is correct immediately:
   ```bash
   python -m orchestrator.migrate_runner --dry-run   # review pending
   python -m orchestrator.migrate_runner             # apply
   ```

**Verify A:** `python -m orchestrator.migrate_runner --dry-run` prints **no
pending migrations** on both the hosted DB and the EC2 DB, and
`\d sandbox_instances` shows a `deleted_at` column.

---

### Task B — Configure orchestrator environment (both tiers)

Set these on the **hosted** orchestrator (`/srv/apps/sandbox-orchestrator/.env`
or its compose `environment:`) and the **EC2** orchestrator (systemd unit env /
SSM), then restart each orchestrator.

| Var | Value | Purpose |
|---|---|---|
| `MATRX_AUTO_MIGRATE` | `1` | Turn on the rolling refresh loop. |
| `MATRX_MIGRATE_MAX_PER_PASS` | `2` (tune) | Cap swaps per 60s sweep so a big drift wave rolls gradually. |
| `MATRX_MIGRATE_RECENT_HEARTBEAT_SECONDS` | `120` (tune) | How long after the last heartbeat a box is still "in use". |
| `MATRX_ACCESS_TOKEN_SECRET` | a strong random secret (**same value across restarts**) | Required for browser/proxy tokens **and** activates the per-sandbox daemon secret. Generate: `python3 -c "import secrets;print(secrets.token_urlsafe(48))"`. |
| `MATRX_ENABLE_S3_MIGRATE` | **leave unset for now** | Enable only after Task E passes. |
| `MATRX_AIDREAM_SERVICE_TOKEN` (EC2 only) | aidream's `AIDREAM_SANDBOX_SERVICE_TOKEN` | Fixes FOUND_DEFECTS Bug 7 (EC2 cloud-files bridge). Hosted reads it from `/srv/projects/aidream/.env` automatically. |

> **Single-worker invariant:** the orchestrator keeps process-local state
> (CWD cache, migration gating, warm-pool bookkeeping, single-use token JTIs).
> **Do not run more than one uvicorn worker** and do not add `--workers N`
> without first moving that state to a shared store. The production Dockerfile
> already pins one worker — keep it that way.

**Verify B:** `GET /` (root) shows the tier; `GET /drift` responds 200 with the
master key; orchestrator logs show `Reaper started` and (if a warm pool is
configured) `Warm pool started`. No `MATRX_API_KEY is not set` warning in prod.

---

### Task C — Rebuild ALL templates on push to main

Confirm/extend the pipeline so **core, slim, aidream, and the `local` starter
image** are all (re)built on a push to main, each version-stamped.

1. **core / slim / aidream** are already built by `deploy-hosted.sh` (hosted) and
   `core`/`slim` by `deploy.yml` (EC2/ECR). **Confirm aidream actually rebuilds**
   on a content change and on "missing" self-heal (`build-aidream.sh`).
2. **`local` starter pool** (`sandbox-local/`, images `matrx-sandbox:local` from
   `:core`, containers `sandbox-1..5`) is **not** currently rebuilt or restarted
   by the deploy. The product owner explicitly wants it in scope. Either:
   - **(preferred)** extend `scripts/deploy-hosted.sh` to also
     `docker build -t matrx-sandbox:local sandbox-local/` and
     `cd sandbox-local && docker compose up -d` when `sandbox-image/` or
     `sandbox-local/` changed or the image is missing; **or**
   - if the starter pool is being retired (it is marked deprecated in
     `CLAUDE.md`), get explicit confirmation and **remove it** instead — don't
     leave it half-maintained.
3. Make sure every template image is **version-stamped** (the SHA build-arg /
   `/etc/sandbox-image-version`) so `GET /drift` can tell old from new. `core`,
   `slim` already pass `MATRX_IMAGE_VERSION`; confirm `aidream` and `local` do
   too (add the build-arg if missing).

**Verify C:** trigger a deploy (push a trivial change or `workflow_dispatch`).
After it completes, on each host `docker images | grep matrx-sandbox` shows
fresh `Created` timestamps for **every** in-scope template, and
`docker exec <a-fresh-box> cat /etc/sandbox-image-version` returns the new SHA.

---

### Task D — Activate and validate the daemon isolation secret

This closes the cross-sandbox hole: today any sandbox can reach another
sandbox's daemon on the shared Docker network. The code is in place and
**fail-open** (no enforcement until the image carries `matrx_agent/api/_auth.py`
**and** the orchestrator injects `MATRX_AGENT_TOKEN`, which it does once
`MATRX_ACCESS_TOKEN_SECRET` is set — Task B).

1. Ensure Task B set `MATRX_ACCESS_TOKEN_SECRET` and Task C rebuilt the images
   (so the daemon has the new `_auth.py`).
2. Create a **fresh** sandbox (so it boots on the new image with the token
   injected).
3. **Prove isolation works** (negative test). From a *second* sandbox/container
   on the same Docker network, hit the first box's daemon directly by IP with
   **no** token:
   ```bash
   curl -s -o /dev/null -w "%{http_code}\n" http://<box1_ip>:8000/fs/list?path=/home/agent
   # EXPECT: 401
   ```
   With the **wrong** token → 401. With **no** token over WebSocket `/pty` →
   closed (1008).
4. **Prove the real path still works** (positive test): a normal tool call
   through the orchestrator proxy (which forwards `X-Matrx-Agent-Token`) and a
   PTY/file-watch WebSocket both succeed. `GET /sandboxes/{id}/diagnostics`
   should be `overall_ok: true`.
5. **Belt-and-suspenders (recommended):** also disable inter-container traffic at
   the network layer. Create a user-defined bridge with ICC off and point the
   orchestrator at it:
   ```bash
   docker network create --opt com.docker.network.bridge.enable_icc=false matrx-sandboxes
   # set MATRX_DOCKER_NETWORK=matrx-sandboxes on the orchestrator, restart, and
   # create a NEW box to confirm orchestrator→daemon + aidream(8001) still work.
   ```
   Validate the proxy, PTY, and aidream paths after switching the network before
   trusting it for all new boxes.

**Verify D:** the negative test returns 401/closed from a peer with no/wrong
token; the positive test (proxied tool call + PTY + diagnostics) succeeds on a
freshly-created box.

---

### Task E — Validate the EC2/S3-ordered migrate, THEN enable it

`MATRX_ENABLE_S3_MIGRATE` is **off** by default because a mis-ordered S3 swap
loses the user's final edits. The path is implemented to be safe (graceful stop
flushes to S3 *before* the new box boots and down-syncs; any pre-cutover failure
restarts the old box, which re-hydrates from the flushed S3). **You must prove
this on a throwaway sandbox before enabling it for real users.**

1. On EC2, create a **throwaway** sandbox. Write a sentinel file with known
   content in `/home/agent` (and, if testing the aidream bridge, a cloud-files
   entry).
2. With `MATRX_ENABLE_S3_MIGRATE=1` set **only in a staging/throwaway
   orchestrator** (or temporarily), drive a migrate of that one box:
   `POST /sandboxes/{id}/migrate`.
3. **Confirm zero data loss:** after the swap, the same `sandbox_id` answers, the
   sentinel file is present with identical content, and the box reports the new
   version. Repeat with a **larger** `/home/agent` (hundreds of MB) to exercise
   the shutdown-flush timeout window.
4. **Fault-injection:** point `target_image` at a deliberately-broken image and
   confirm the migrate **fails, the OLD box is restarted, and the sentinel data
   is intact** (rollback works).
5. Only after 1–4 pass repeatedly: set `MATRX_ENABLE_S3_MIGRATE=1` on the real
   EC2 orchestrator.

> **Known caveat to watch:** the in-container shutdown flush caps `hot-sync up`
> at `SHUTDOWN_TIMEOUT_SECONDS` (default 30s). For very large homes the flush can
> be truncated (audit finding #9). If your test in step 3 shows truncation,
> raise `MATRX_SHUTDOWN_TIMEOUT_SECONDS` for EC2 boxes before enabling, and note
> it.

**Verify E:** migrate of a throwaway EC2 box preserves data byte-for-byte across
several runs including a forced-failure rollback; only then is the flag enabled.

---

### Task F — Prove the full loop end to end

With A–E done and `MATRX_AUTO_MIGRATE=1`:

1. Create 2 sandboxes per tier. Keep **one busy** (run a long `sleep`/stream and
   send heartbeats) and leave **one idle**.
2. Push a trivial change to `main` (or `workflow_dispatch`) to rebuild templates.
3. Watch the orchestrator logs / `GET /drift`:
   - within ~60–120s the **idle** boxes migrate (`MIGRATED ...`), keep their
     `sandbox_id`, and report the new version;
   - the **busy** box logs `busy_deferred` each sweep and is **not** interrupted;
   - when you stop the activity + heartbeats, the busy box migrates on a
     subsequent sweep.
4. Confirm user data survived every swap (a sentinel file written before the
   push is present after).

**Verify F:** drift goes to zero for idle boxes within a couple of sweeps, the
busy box is never interrupted and migrates once idle, and no data is lost — on
**both** tiers.

---

## 3. <a id="victory-checklist"></a>Victory Checklist — loop until ALL green

Do not declare done until every box passes. Re-run after any change.

- [ ] **A.** `migrate_runner --dry-run` shows no pending migrations on hosted +
      EC2 DBs; `deleted_at` column exists; runner is wired into both deploy paths.
- [ ] **B.** `MATRX_AUTO_MIGRATE=1`, `MATRX_ACCESS_TOKEN_SECRET`, idle-window, and
      (EC2) `MATRX_AIDREAM_SERVICE_TOKEN` are set on both orchestrators;
      single-worker preserved; reaper running.
- [ ] **C.** A push to main rebuilds **core, slim, aidream, and local** (or local
      is explicitly retired), all version-stamped; fresh boxes report the new SHA.
- [ ] **D.** Peer-with-no-token gets 401/closed from a box's daemon; the real
      proxied tool path + PTY + diagnostics still succeed; (optional) ICC-off
      network validated.
- [ ] **E.** S3-ordered migrate validated on a throwaway EC2 box (incl.
      large-home + forced-failure rollback) with **zero data loss**, then
      `MATRX_ENABLE_S3_MIGRATE=1` enabled.
- [ ] **F.** End-to-end: push → idle boxes roll within ~2 sweeps, busy box never
      interrupted and rolls once idle, data preserved, **on both tiers**.
- [ ] **G.** `GET /drift` reads **zero drifted** on both tiers after a deploy
      settles (modulo boxes legitimately busy at that moment).
- [ ] **H.** A second consecutive push to main also rebuilds + rolls cleanly
      (proves it's repeatable, not a one-off).

---

## 4. Safety notes & rollback

- **Orchestrator is the critical path.** `deploy-hosted.sh` already tags
  `:rollback` and restores the last-known-good container on health failure.
  Don't remove that. The EC2 deploy has **no** rollback yet (audit #24) — if you
  touch it, mirror the hosted rollback pattern.
- **Never enable `MATRX_ENABLE_S3_MIGRATE` before Task E passes.** A wrong
  ordering here is the single most likely data-loss bug in the system.
- **Token secret stability:** `MATRX_ACCESS_TOKEN_SECRET` doubles as the daemon
  secret seed. If you rotate it, in-flight tokens **and** the daemon secrets of
  already-running boxes change — roll/refresh boxes after a rotation.
- **Quiesce expectation:** the migrate primitive only protects *idle* boxes
  automatically. If a caller (aidream) can park a turn, it should before driving
  a manual `/sandboxes/{id}/migrate`; the rolling auto-migrate already only
  touches idle boxes.
- If a deploy floods drift and you want to throttle, lower
  `MATRX_MIGRATE_MAX_PER_PASS`; to pause rolling entirely, set
  `MATRX_AUTO_MIGRATE=0` (manual `/migrate-all` still works).

## 5. Reference

- Rolling logic: `orchestrator/reaper.py`, `orchestrator/migrate.py`
  (`migrate_sandbox`, `_migrate_s3_ordered`, `migrate_all_drifted`).
- Daemon secret: `sandbox-image/sdk/matrx_agent/api/_auth.py`,
  orchestrator `sandbox_manager.agent_token_for` / `agent_forward_headers`.
- Migration runner: `orchestrator/migrate_runner.py`,
  `orchestrator/migrations/*.sql`.
- Deploy: `.github/workflows/deploy.yml`, `scripts/deploy-hosted.sh`.
- Drift / migrate endpoints: `GET /drift`, `POST /migrate-all`,
  `POST /sandboxes/{id}/migrate`, `GET /sandboxes/{id}/diagnostics`.
- Background on zero-drift: `docs/ZERO_DRIFT.md`. Persistence model:
  `docs/PERSISTENCE_PLAN.md`. EC2 cloud-files defect: `FOUND_DEFECTS.md`.
