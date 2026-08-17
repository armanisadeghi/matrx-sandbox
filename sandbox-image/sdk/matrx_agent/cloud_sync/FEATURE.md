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

## Entry points

- `watcher.py::CloudFilesWatcher` — live bidirectional replica and durable event replay.
- `client.py::AsyncBridgeClient` — authenticated AI Dream bridge client.
- `cli/files.py` — `mtx files` commands and bulk startup/shutdown safety passes.
- `scripts/cloud-files-sync.sh` — lifecycle wrapper installed in the sandbox image.

## Verification

- `pytest sandbox-image/sdk/tests/test_cloud_sync_boundaries.py`

## Change log

- 2026-08-17 — Excluded canonical system paths at every local ingress and
  replay point, and stopped retrying permanent 4xx responses. This closes the
  loop that repeatedly tried to overwrite immutable scraper evidence mirrored
  into persistent sandbox volumes.
