# Cloud-files: status & remaining work

**Last updated:** 2026-04-29

## Status snapshot

What's running on a freshly-spawned hosted-tier sandbox today (verified against
`sbx-b4ee767ab06d` at 05:29 UTC, calling
`https://server.app.matrxserver.com/api/cloud-files/integrations.aidream`):

```
features:
  put_dedup_by_checksum:        true
  path_normalization:           true
  polling_changes_endpoint:     true
  realtime_changes_publication: false   ← NOT YET — see Tier B-finish below
endpoints: list, get, put, delete, quota, changes, integrations.aidream
watcher mode: active · subscriber: PollingSubscriber · ai_dream_configured: true
```

## What's done (one-liners)

These all shipped, verified, and are running on the live hosted tier. Don't
re-explain them; check the linked commits if you need detail.

- **Up-direction watcher** — `e8cb21b` (Apr 28). Real-time `~/cloud-files/` →
  `cld_files` via `/api/cloud-files/put` with 5 s debounce.
- **Sandbox layout codified** — `d55373f`. `~/.matrx/`, `cloud-files/`,
  `repos/`, `projects/`, `scratch/` created on every boot via
  `ensure-layout.sh` plus the agent-facing `SANDBOX_LAYOUT.md`.
- **Crash-resilient retry queue (A1)** — `ead8cc2`.
  `~/.matrx/runtime/cloud-sync-queue.jsonl` with replay + size-bounded
  compaction.
- **Self-healing startup (A2)** — `ead8cc2`. Modes
  `dormant|waiting|degraded|active` with exp-backoff retry; degraded mode
  proceeds when the bridge probe succeeds but the down-marker is missing.
- **Status endpoint (A3)** — `ead8cc2`. `GET /internal/cloud-sync-status` on
  matrx_agent (port 8000).
- **Session-report splice (A4)** — `ead8cc2`. `manifest.cloud_sync` carries
  watcher stats; rendered as a "Cloud-files sync (last session)" section.
- **Bridge dedup + path-safety (A5/A6)** — aidream `4d6c2df`. SHA-256 short-
  circuit returns existing record with `"deduped": true`; every endpoint runs
  `file_path` through `common.path_safety.sanitize_logical_path`.
- **Down-direction polling (B1, polling half)** — sandbox `3836f2f` + aidream
  `4d6c2df`. `GET /api/cloud-files/changes?since=` polled every 30 s by
  `PollingSubscriber`. Surfaces modifications only — deletions are explicitly
  out of scope for the polling path.
- **Echo-loop guard (B1)** — `3836f2f`. `_apply_remote_change` updates
  `_last_hash` so the watchdog event for our own write de-dups in
  `_flush_upsert`; the `recently_applied` LRU is the second line.
- **Boot reconciliation against `cld_files`** — `9f1be64`. Real bug found
  in production: persistent-volume files from a crashed prior session were
  never uploaded; new `_reconcile_against_remote` calls `client.list_files()`
  and queues missing-or-checksum-divergent files through the same debounce.
- **`BridgeClient` Protocol + dual implementations** — Batch B `8f166cb`
  + `9511565`. `LocalFilesClient` for `:aidream` (full `/files/*` surface,
  presigned uploads, versioning) vs `RemoteBridgeClient` for `:core`/`:local`.
  `select_bridge_client(cfg)` picks at startup. `list_changes` lives on the
  Protocol now.
- **Path-traversal guard on bulk down-sync (A7)** — Batch B `8f166cb`.
- **`mtx files versions/restore/diff` CLI** — Batch B `8f166cb`.
- **Audit logger module (`matrx_agent.audit.write_action_log`)** — Batch B
  `8f166cb`. Helper exists; **the watcher does not call it yet** — see A8
  below.

## What's still pending

Roughly in order of value-per-effort. Each item names the seam that's
already in place so the work is constrained.

### Tier B-finish — flip Realtime on

Polling at 30 s is fine for the average case but Realtime is the actual
"feels live" experience the layout doc promises. All the in-sandbox code is
shipped and dormant; turning it on is three operator steps + one image rebuild.

1. **Apply the publication migration.**
   ```bash
   psql "$SUPABASE_URL" -f db/migrations/0002_cld_files_realtime.sql
   ```
   Idempotent. Adds `cld_files` to `supabase_realtime` and sets
   `REPLICA IDENTITY FULL` so UPDATE payloads carry the previous row (lets
   us distinguish soft-delete from a normal edit). Once applied, flip the
   advertised feature flag in `cloud_files_bridge.integrations_aidream` from
   `realtime_changes_publication: false` to `true` so operators can see it
   from the wire.

2. **Add the `realtime` Python lib to `:core`.**
   `sandbox-image/sdk/pyproject.toml` → add `realtime>=2.0`. Rebuild
   `:core`/`:local`/`:aidream` via the existing scripts. Without it,
   `_realtime_available()` returns False and the watcher silently stays on
   polling forever (currently observed: `subscriber: "PollingSubscriber"`
   even on `:aidream` images).

3. **Confirm the orchestrator passes Supabase env vars.**
   `orchestrator/orchestrator/config.py::aidream_passthrough_env` already
   lists `SUPABASE_URL,SUPABASE_KEY,...`. Verify those are populated in the
   orchestrator's own env (`/srv/apps/sandbox-orchestrator/.env`); add
   `SUPABASE_ANON_KEY` if you want anon-key Realtime auth instead of
   service-role.

4. **Verify on a fresh sandbox.** `psql ... UPDATE cld_files SET ...` from a
   sibling session → file appears in `~/cloud-files/` within ~1 s; status
   shows `downstream.subscriber == "RealtimeSubscriber"` and `applied > 0`;
   no echo PUT logged. The PollingSubscriber stays as graceful-degradation
   when the WebSocket drops — expected behaviour, no change needed.

### A8 — wire the audit logger into the watcher

The helper exists (`matrx_agent.audit.write_action_log`), but the watcher
fires PUT/DELETE/GET without writing markdown records to
`~/.matrx/runtime/tool-calls/_runtime/`. Means the agent's audit panel
shows shell + python history but cloud-sync activity is invisible.

- Call site: end of `_flush_upsert` (success and final-failure branches),
  end of `_flush_delete`, end of `_apply_remote_change` for both
  modify and delete paths.
- Tool names to use so the FE filter groups them:
  `cloud_sync_put`, `cloud_sync_delete`, `cloud_sync_download`.
- Frontmatter: `path`, `bytes`, `version`, `latency_ms`, `success`,
  `cause: watcher|reconcile|downstream`. Shape already matches what
  `audit.write_action_log` expects.
- Fire-and-forget — never block the watcher's flush on a log write.

Half a day of work; trivially testable by tailing
`~/.matrx/runtime/tool-calls/_runtime/` and triggering a few writes.

### Multi-sandbox concurrency model

Two sandboxes for the same user can clobber each other's writes. Last-
writer-wins because nothing exchanges a precondition.

The right primitive is `If-Match: <prev_version>` on bridge PUT — the bridge
returns 412 when the version has moved since the watcher last saw it; the
watcher catches the 412 and re-queues the file with a fresh local read so
the next PUT cites the now-current `prev_version`. Pairs naturally with the
Realtime path: the version received via Realtime is what the next PUT cites.

Open question on resolution policy when 412 fires: keep both copies under a
`<path>.<sandbox_id>.conflict` filename, or just refuse and surface in the
session report? Lean toward conflict-file since silently refusing means the
agent's most recent edit disappears.

Estimate: 1–2 days. Not blocking for v1; risk grows with concurrent sandbox
adoption.

### Quota gaps & enforcement

Real bugs called out in the original analysis but punted because none breaks
the happy path:

- **Soft-deleted files don't count toward quota.** `_used_bytes()` filters
  `deleted_at IS NULL`. User soft-deletes 9 GiB, uploads 9 GiB more →
  18 GiB on a 10 GiB plan. Either count soft-deleted rows for some retention
  window, OR add a real trash/restore surface that hard-purges after N days.
  Lean toward the latter; the schema already supports it.
- **Quota check race.** `_used_bytes` then `managed_write_async` is TOCTOU.
  Two concurrent PUTs can both pass and collectively overflow. Fix: a
  Postgres advisory lock per `owner_id` around the write, or move to a
  serialisable transaction. Same fix on the full `/files/*` router.
- **No checksum validation on GET.** `cld_files.checksum` is stored, never
  verified on read. Compute SHA-256 on returned bytes and compare; 502
  (or stream a re-fetch) on mismatch. Cheap.
- **Memory buffering on PUT.** `await file.read()` puts the whole upload in
  RAM. For `:core` images that can't use the presigned path, a 1 GiB upload
  × N concurrent workers OOMs aidream. Stream the bytes through a hash +
  boto3 multipart writer instead; touches the bridge only.

Each is ~half a day. Best done before opening the bridge to a wider tenant
pool.

### Bulk-delete coalescing

`rm -rf ~/cloud-files/big-dir/` with N files = N debounced DELETEs. The
bridge would benefit from a `DELETE /api/cloud-files/delete-batch` taking
a JSON array of paths; the watcher could collapse adjacent deletes within
a debounce window into a single call. Nice-to-have; not blocking.

### Webhook worker for `cld_events`

Out of scope for now. Realtime gives us live updates; `cld_events` is the
authoritative audit trail and would be useful for "what did this user
touch this month" reports, but no one reads it today and Realtime covers
the agent-visible path. Park it.

## File map (where things live)

The split between Batch A (watcher + bridge + Realtime) and Batch B (SDK
client abstraction + agent-facing tooling) is preserved in these paths.
Use it for ownership when picking up a follow-up.

```
matrx-sandbox/
  sandbox-image/sdk/matrx_agent/
    cloud_sync/
      watcher.py        ← Batch A (modes, queue, reconcile, downstream wiring)
      queue.py          ← Batch A (PersistentQueue)
      downstream.py     ← Batch A (PollingSubscriber, RealtimeSubscriber)
      client.py         ← Batch B (BridgeClient Protocol + Local/Remote impls)
    api/main.py         ← /internal/cloud-sync-status, lifespan glue
    audit.py            ← Batch B (write_action_log helper — A8 not yet wired)
    persistence/
      manifest.py       ← cloud_sync field on SessionManifest
      session_report.py ← cloud-sync section renderer

aidream/
  aidream/api/routers/cloud_files_bridge.py
                        ← list/get/put/delete/quota/changes/integrations
  common/path_safety.py ← sanitize_logical_path (used by bridge + /files/*)
  db/migrations/0002_cld_files_realtime.sql
                        ← Realtime publication — NOT YET APPLIED
```

## Verification checklist for follow-ups

For anyone picking up the pending items, these are the smoke tests that
confirm the seam still holds. None require special infra beyond a running
hosted-tier sandbox and `psql` access to the cld_files DB.

- `curl 127.0.0.1:8000/internal/cloud-sync-status` → mode `active`,
  ai_dream_configured `true`, subscriber non-null.
- Touch a file under `~/cloud-files/` → within ~6 s the bridge logs a PUT
  (or returns `deduped: true` for an unchanged write).
- After Realtime flip: `psql … UPDATE cld_files SET … WHERE …` →
  `~/cloud-files/` content updates within ~1 s, status counter
  `downstream.applied` increments, no echo PUT in `puts` counter.
- After A8 wires: `ls ~/.matrx/runtime/tool-calls/_runtime/` shows
  `<ts>-cloud_sync_put-<id>.md` records with sane frontmatter.
