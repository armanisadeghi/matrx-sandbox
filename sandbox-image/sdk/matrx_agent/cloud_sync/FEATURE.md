# Cloud-files sandbox replica

Verified against `watcher.py`, `client.py`, `paths.py`, `cli/files.py`, and the
AI Dream bridge on 2026-08-17.

## Contract

`/home/agent/cloud-files` is a writable replica of the user's ordinary file
tree. Startup pulls remote bytes, the watcher synchronizes changes in both
directions, and shutdown performs a bulk up-sync safety pass.

**System-managed paths never enter this replica.** `generations/` and
`system-files/` are application artifacts, not user workspace paths. The server
excludes both roots before pagination. `paths.py::is_system_path` independently
guards the CLI, seed walk, filesystem event drain, persisted-event replay, and
remote apply path. A stale system artifact already present on a persistent
volume remains ignored and can never be uploaded, overwritten, or deleted by
the replica.

**Permanent rejections are attempted once.** The watcher retries timeouts,
rate limits, and 5xx responses. Other 4xx responses are caller/policy conflicts;
retrying the identical request cannot heal them and is forbidden. The event is
retired from the durable queue after the one loud error record.

**Local blocking work stays off the event loop.** Tree hashing and per-file
hashes run through `asyncio.to_thread`. Filesystem observer callbacks hand work
to the loop with `call_soon_threadsafe` and never perform network I/O.

**The server sets the polling cadence; this box follows it.** Realtime is the primary
down-direction path and `/api/cloud-files/changes` is the fallback. Every answer from that
endpoint carries `poll_after_seconds` (mirrored in the `Retry-After` header) — 30 s normally,
45 s while AI Dream's change feed is shedding — and `downstream.PollingSubscriber` adopts it for
its next wait, jittered like every other wait here. An instruction outside 5–600 s is refused with
a warning and the built-in 30 s is kept; an answer with no instruction (an older bridge) changes
nothing. This is the fleet half of the 2026-09-14 re-sizing: 226 boxes on fixed 30 s timers arrive
at 7.5 polls/s, which is more than the server's bounded feed can serve — following the hint takes
the fleet to 5.02/s the moment anyone is shed. Server side:
`aidream/services/sandboxes/change_feed_admission.py` + knobs `infrastructure.sandbox` /
`change_feed_*`.

## Entry points

- `watcher.py::CloudFilesWatcher` — live bidirectional replica and durable event replay.
- `client.py::AsyncBridgeClient` — authenticated AI Dream bridge client.
- `cli/files.py` — `mtx files` commands and bulk startup/shutdown safety passes.
- `scripts/cloud-files-sync.sh` — lifecycle wrapper installed in the sandbox image.

## Verification

- `pytest sandbox-image/sdk/tests/test_cloud_sync_boundaries.py`
- `pytest sandbox-image/sdk/tests/test_downstream_retry_after.py`

## Change log

- 2026-09-12 — The polling fallback honours the bridge's `Retry-After` on a shed poll
  (`503 cloud_file_change_feed_shed` / `429`, clamped to the backoff ceiling) and jitters every
  wait by ±20% so a fleet started together stops polling AI Dream in the same second. Evidence:
  43 sandboxes polled inside one second three times on 2026-09-12 and consumed the server pool.
- 2026-08-17 — Excluded canonical system paths at every local ingress and
  replay point, and stopped retrying permanent 4xx responses. This closes the
  loop that repeatedly tried to overwrite immutable scraper evidence mirrored
  into persistent sandbox volumes.
