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

**Every call carries BOTH halves of the request context.** A sandbox is one of
our servers acting for a person, so each bridge call sends `X-Matrx-User-Id`
AND `X-Organization-Id` (the organization the create request named, injected by
the orchestrator as `ORGANIZATION_ID`). The headers are built in ONE place —
`matrx_agent/bridge_headers.py` — and a container missing either variable
REFUSES the call naming the missing variable; `BridgeConfig.from_env()` returns
None and `report_missing()` lists exactly what is absent. AI Dream refuses a
call without the organization (`400 organization_required`) because the write
below it would otherwise land in the person's PERSONAL organization — which is
what happened to every sandbox write before 2026-09-17. Law:
`common-docs/policies/context-is-carried-never-rebuilt.md` rule 1.

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
- `bridge_headers.py::identity_headers` — THE ONE builder of the identity
  headers for every AI Dream call in this image (shell half:
  `scripts/bridge-headers.sh`).
- `cli/files.py` — `mtx files` commands and bulk startup/shutdown safety passes.
- `scripts/cloud-files-sync.sh` — lifecycle wrapper installed in the sandbox image.

## Verification

- `pytest sandbox-image/sdk/tests/test_cloud_sync_boundaries.py`
- `pytest sandbox-image/sdk/tests/test_downstream_retry_after.py`

## Change log

- 2026-09-17 — **The organization now crosses the boundary.** Identity headers
  moved into one builder (`bridge_headers.py` / `scripts/bridge-headers.sh`) and
  every sandbox → AI Dream call sends `X-Organization-Id` beside
  `X-Matrx-User-Id`: cloud-files clients, `mtx files`, the Browser Manager
  client, and the git-credential helper (which previously sent the user only).
  A box missing `ORGANIZATION_ID` refuses the call by name instead of omitting
  the header. AI Dream flipped from warning to refusing (400
  `organization_required`) in the same wave, so an image built before this
  change fails loudly with the remedy — rebuild/redeploy — rather than writing
  into the wrong tenant. Guard:
  `tests/test_cloud_sync_boundaries.py::test_no_second_header_builder_exists_in_the_image`
  (proven failing-then-passing) plus the on-the-wire header assertion beside it.
- 2026-09-12 — The polling fallback honours the bridge's `Retry-After` on a shed poll
  (`503 cloud_file_change_feed_shed` / `429`, clamped to the backoff ceiling) and jitters every
  wait by ±20% so a fleet started together stops polling AI Dream in the same second. Evidence:
  43 sandboxes polled inside one second three times on 2026-09-12 and consumed the server pool.
- 2026-08-17 — Excluded canonical system paths at every local ingress and
  replay point, and stopped retrying permanent 4xx responses. This closes the
  loop that repeatedly tried to overwrite immutable scraper evidence mirrored
  into persistent sandbox volumes.
