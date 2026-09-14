# 2026-09-13 — the edge dropped a healthy hosted orchestrator for ~7 minutes

`https://orchestrator.dev.codematrx.com` returned Traefik `503 no available
server` from 06:40:45 to ~06:47:20 UTC (with hard timeouts in the middle). The
orchestrator **process never restarted** — `/health` at 06:47:20 reported
`uptime_seconds` 3010, and `docker inspect` still shows
`StartedAt 2026-09-13T05:57:08Z` for container `7572e8244fa8`. A live, correct
process was removed from the edge.

## What actually happened

The 60-second liveness reconcile takes one deployment-shared migration lease per
sandbox **on the asyncio event loop**. Each lease is blocking filesystem work:
four `flock` acquisitions plus `HostedMigrationJournal.ensure_ready()`, which
writes and `fsync`s a probe file — and `ensure_ready` ran once per lock *and*
again for the per-sandbox `pending()` read, so five disk syncs per sandbox. With
214 hosted sandboxes that is ~1,070 fsyncs and ~214 journal-directory globs per
tick, all on the loop that serves `/health`.

The concurrent `matrx-sandbox:aidream` image build (06:28:47 → 06:53:57, queued
by the 283307b / 0c43afe pushes) drove the host into sustained iowait, so each
fsync became slow. The loop went dark: the orchestrator's own access log shows
`/health` responses at 06:40:36 and 06:41:08, then **nothing until 06:46:56**,
when fourteen queued probes all completed inside one second — the signature of a
blocked loop draining its backlog — immediately before
`Liveness reconcile complete (tier=hosted): stopped=0 refreshed=213` at 06:46:57.

The container healthcheck gives `/health` a 3-second budget (`interval 30s`,
`timeout 5s`, `retries 3`). Probes crossed it repeatedly from 06:36:45 (3015 ms)
and 06:39:30 (3476 ms) and then failed outright through the stall. Three
consecutive failures marked the container `unhealthy`, and **Traefik v3's Docker
provider routes only to containers whose health is `healthy`** — with one
replica, dropping it leaves the router with no server at all: `503 no available
server`. Recovery at 06:47:20 was simply the next probe passing (416 ms).

The deploy is **not** the cause. `matrx-hosted-deploy.service` built the
orchestrator candidate at 06:28:40, spent 06:28:47–06:53:57 on the aidream image,
ran migrations (`applied=0 already=6`) at 06:54:07, and then **rolled back**
at 06:54:09 (`ERROR: active sandbox migration deferred hosted promotion`). It
never recreated `matrx-orchestrator`. Its only contribution was the I/O load.

This was chronic, not a one-off: at 08:24–08:36 UTC, with no build running, the
same "60-second" sweep was taking 18–20 minutes per tick
(complete at 07:47:58, 08:07:12, 08:24:57) and the edge was flapping — three
consecutive `curl` samples returned 503 at 08:36 while `docker inspect` read
`unhealthy`, and 200 whenever it read `healthy`.

## Timeline (UTC)

| Time | What | Source |
|---|---|---|
| 05:57:08 | `matrx-orchestrator` starts (SHA 8d56845); never restarted since | `docker inspect .State.StartedAt` |
| 06:28:40 | Deploy poller picks up target 11b37de | `journalctl -u matrx-hosted-deploy.service` |
| 06:28:47 | aidream image build begins (~25 min, heavy I/O) | same |
| 06:36:45 | `/health` 3015 ms — first probe over the 3s budget | orchestrator access log |
| 06:39:30 | `/health` 3476 ms | same |
| 06:41:08 | Last `/health` served before the stall (1884 ms) | same |
| ~06:40:45 | Edge begins returning `503 no available server` | reporter |
| 06:41:08–06:46:56 | **No `/health` responses at all** — event loop blocked | same |
| 06:46:56 | 14 queued `/health` probes complete within one second | same |
| 06:46:57 | `Liveness reconcile complete: stopped=0 refreshed=213` | orchestrator log |
| 06:47:20 | Edge back to 200; `uptime_seconds` 3010 (same process) | reporter |
| 06:53:57 | aidream build finishes | deploy journal |
| 06:54:09 | Deploy **rolls back**; orchestrator never recreated | deploy journal |

## The fix (class, not instance)

`reconcile.py` now takes the whole fleet's leases inside one
`asyncio.to_thread`, so blocking journal/lock I/O can never starve `/health`
again, and reads the journal's pending set **once** per sweep instead of once per
sandbox. `hosted_operation_lease_sync` is the real (blocking) lease;
`hosted_operation_lease` is a thin async wrapper for single operations.
`HostedMigrationJournal.probed()` proves the state root writable once per lease
acquisition instead of five times — the four locks hit the same root microseconds
apart, so the extra syncs bought nothing; a long migration still re-proves the
mount at every fresh acquisition. Lease semantics, exclusions and denials are
unchanged.

The same class was censused across the file and closed in its two siblings: the
discovery sweep (`reconcile_from_docker`) and the zombie reap take a per-container
lease and read the migration fence for every container they find — both now run
off the loop. They already awaited between containers, so their exposure was one
container's blocking work at a time rather than a fleet's, but with a slow disk
that is still ~1 s of dead air per container.

Guards: `orchestrator/tests/test_liveness_reconcile_event_loop.py` leases a
120-sandbox fleet with realistically slow locks and asserts the event loop keeps
ticking. On the pre-fix code the heartbeat gets **zero** ticks for the whole
sweep; after the fix it never stalls beyond 300 ms. A second guard does the same
for the discovery sweep (pre-fix: a 0.97 s stall).

## 2026-09-14 follow-up — what landed, and what the numbers are now

The reconcile fix is **live**: the hosted orchestrator runs `eff96e4` (its own
`/app/.source-sha`), the 32 MB `sbx-7a395dcdd163` record was retired to
`.completed-20260913T2158Z` by `cff52ef`, the release gate is no longer
deadlocked (8 successful promotions in 12 h), and `/health` answered **200 on
164/164 samples over 888 s** at 5 s intervals — zero unavailability, against 37 %
availability during the incident. Note that window contained no orchestrator
recreation, so it does not prove the edge survives one.

Two things the fix did not close, both measured on 2026-09-14:

- **The I/O load itself.** The poller rebuilt the ~6 GB aidream *template*
  image on every tick that saw a new aidream commit — ~40 builds in 12 h — and
  each completed build took the promotion barrier that stops and recreates the
  single-replica orchestrator (8 times, container down 2–17 s each, median 11 s;
  Traefik only routes to a `healthy` container, so the edge gap is at least
  that). Closed by `b0f9764`: the cadence is now the knob
  `AIDREAM_REBUILD_MIN_INTERVAL_SECONDS` (6 h default). See
  [OPERATIONS.md](../OPERATIONS.md).
- **The sweep still outlasts its tick**, exactly as predicted below. Measured
  from `Liveness reconcile complete` timestamps 07:34–07:57 UTC against the
  fixed 60 s `REAP_INTERVAL_SECONDS`: **min 44 s, median 164 s, max 170 s** per
  reap tick. The host carries **215 running sandbox containers, 212 of them
  created in August**, at load average 8.3 on 8 cores. That fleet — not the
  reconcile code — is now the cost. `REAP_INTERVAL_SECONDS = 60` is also a
  hardcoded constant rather than a knob (`orchestrator/reaper.py:54`).

Still open after this pass, and named rather than fixed: the release gate's
bootstrap path still `docker pause`s the live orchestrator *before* it knows it
can seize the locks (`scripts/deploy-hosted.sh`, `acquire_frozen_old_locks`),
which is the unfinished half of the 2026-09-13 ruling; and the safe promotion
path's deployment-lock acquisition is still non-blocking, so it loses ~40 % of
ticks to the sweep. Neither fired destructively on 2026-09-14.

## What is still true and worth knowing

- The hosted orchestrator is a **single replica behind a health-gated router**.
  Anything that makes it slow — not just dead — removes it from the edge
  entirely. Any new background sweep must stay off the event loop.
- The reconcile still does real per-sandbox filesystem work; it is now merely
  invisible to HTTP. If a sweep starts outlasting its 60s tick again, that is the
  next thing to fix, not the healthcheck budget.

## Why the sweep got worse after 07:25, and why the fix could not be deployed

At 07:25:51 a hosted migration for user sandbox `sbx-7a395dcdd163` stalled at
phase `activation_intent` and wrote a **32 MB** journal record (it carries a full
backup manifest). `_pending_conflict` re-read the whole journal directory for
**every** sandbox, so from that moment each 60-second sweep parsed ~7 GB of JSON:
sweeps stretched to 18–20 minutes (complete at 07:47:58, 08:07:12, 08:24:57,
08:43:45, 09:02:44, 09:21:58, 09:42:18) and the orchestrator was effectively
sweeping continuously. Measured on the live edge at 5-second intervals from
08:46:41 to 10:02:38 UTC: **317 of 867 samples returned 200 (37%)**, with
unavailable runs of 377 s, 333 s, 330 s, 330 s and 323 s. Reading the pending set
once per sweep cuts that work by ~214×.

That stalled record also wedged the release gate, so the fix above is committed,
CI-approved onto `deploy/hosted` (0a7f5d6) and **not yet live**:

- Five consecutive poller runs failed at promotion and rolled back (06:54:09,
  07:58:05, 08:36:17, 09:26:49, and the 09:28 run). Two distinct refusals:
  `active sandbox migration deferred hosted promotion` (the nonterminal record)
  and `old-source migration/recovery lock contention deferred hosted promotion`
  (the sweep holds the shared `deployment` lock ~18 of every 19 minutes, so the
  gate's non-blocking exclusive acquisition essentially never wins).
- When the starved orchestrator fails the gate's contract probe, the gate takes
  the bootstrap path, which `docker pause`s the live orchestrator *before* it
  knows it can seize the locks — a paused orchestrator cannot release them, so
  that path is guaranteed to fail here, and it takes the edge down for the
  attempt (08:36:17 and 09:26:49 both logged
  `failed to resume exact pre-promotion orchestrator`).

**The deadlock is self-referential: the defect holds the lock the release gate
needs to deploy the fix for it.** Breaking it needs one of two decisions that
were outside this task's scope: recovering the stuck `sbx-7a395dcdd163` migration
(a user sandbox), or changing the release gate's lock semantics (a bounded wait
on the safe path, and never pausing the live orchestrator before the seize).
