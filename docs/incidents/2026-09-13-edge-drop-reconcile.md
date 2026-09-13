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

Guard: `orchestrator/tests/test_liveness_reconcile_event_loop.py` leases a
120-sandbox fleet with realistically slow locks and asserts the event loop keeps
ticking. On the pre-fix code the heartbeat gets **zero** ticks for the whole
sweep; after the fix it never stalls beyond 300 ms.

## What is still true and worth knowing

- The hosted orchestrator is a **single replica behind a health-gated router**.
  Anything that makes it slow — not just dead — removes it from the edge
  entirely. Any new background sweep must stay off the event loop.
- The reconcile still does real per-sandbox filesystem work; it is now merely
  invisible to HTTP. If a sweep starts outlasting its 60s tick again, that is the
  next thing to fix, not the healthcheck budget.
