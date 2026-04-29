# Cloud-files: current state, gaps, and execution plan

## Part 1 — The two systems

There are two distinct cloud-files HTTP surfaces in the AIDream codebase that do very different things:

| | Bridge `/api/cloud-files/*` | Full router `/files/*` |
|---|---|---|
| Lives in | `aidream/api/routers/cloud_files_bridge.py` | `aidream/api/routers/files.py` |
| Endpoint count | 5 (list, get, put, delete, quota) | 45+ |
| Auth model | Service token + `X-Matrx-User-Id` header (sandbox-friendly) | User JWT (browser-friendly) |
| Purpose | Minimal "blob R/W" surface for sandboxes | Full-fat product UI |
| Versioning | None — every PUT silently bumps version | List versions, get version content, restore version, diff versions |
| Permissions | None | Grant/revoke/list, group ACLs, share-links with TTL + max-uses |
| Folders | None (paths are flat strings) | Tree view, create/delete/move folder, list folders |
| Bulk ops | None | Batch upload, bulk-delete |
| Search | None | Full-text + metadata filter |
| Uploads | Buffered in memory (1 GiB cap, OOM risk under concurrency) | Presigned-URL flow → browser PUTs S3 directly, zero buffering |
| Trash / restore | Soft-delete only (set `deleted_at`) | Full trash bin: list, restore, empty |
| Real-time | None | Wired for `cld_events` audit emit (worker not yet built) |

Both routers are mounted on every `:aidream` sandbox at `127.0.0.1:8001` — the in-sandbox AI Dream FastAPI exposes both surfaces. The watcher we just shipped uses the lean bridge regardless.

Both routers ultimately call the same `SyncEngine` (`packages/matrx-utils/.../sync_engine.py`), so the data model is identical — the only thing changing is which HTTP API you hit and what gets persisted.

`SyncEngine` itself is much richer than either router exposes. It has ~100 public methods including `track_write` / `fire_and_forget_write` (built for exactly the auto-sync use case we're solving), `managed_get_url` (presigned download URLs for skipping sandbox egress on reads), `restore_version`, `diff_versions`, group/share-link permission ops, etc.

---

## Part 2 — What the bridge does poorly that the full router does well

**Bridge weaknesses:**

1. **PUT memory buffering.** `cloud_files_bridge.py:143` does `content = await file.read()` — the entire upload sits in RAM. 1 GiB × N concurrent uploads → OOM. The full router has a presigned flow (`POST /files/upload/presigned` → browser PUTs S3 directly → `POST /files/finalize-upload`) that buffers nothing.
2. **Quota check race.** `_used_bytes()` then write — TOCTOU. Two concurrent PUTs can both pass the quota check then collectively exceed it. The full router has the same pattern but it's also wrong there.
3. **No content-hash dedup.** Every PUT, even of identical bytes, creates a new `cld_file_versions` row. With our shutdown bulk up-sync running plus the new watcher, this generates a lot of useless versions.
4. **No checksum validation on GET.** `cld_files.checksum` is stored, never verified on read — silent corruption goes undetected.
5. **Path normalization missing.** `foo//bar`, `foo/./bar`, trailing slashes — all accepted. `SyncEngine` trusts the caller. Two paths can resolve to the same logical file.
6. **Soft-deleted files don't count toward quota.** `_used_bytes()` filters `deleted_at IS NULL` — soft-delete 9 GiB of files, upload 9 GiB more, you've now stored 18 GiB on a 10 GiB plan.

**Full-router strengths the bridge lacks:**

7. **Version history surface** — list/get/restore/diff. With the bridge, the version column advances but is invisible to the sandbox.
8. **Folders as first-class.** Bridge treats path as opaque string; full router has a tree view, folder ops, and folder permissions.
9. **Sharing & ACLs.** Grant another user read/write on a single file, or generate a share-link with TTL/max-uses. Bridge has no concept.
10. **Search.** The bridge's `?prefix=` is the only filter. The full router has full-text + mime-type + tag.
11. **Trash bin.** Soft-deletes are recoverable from `/files/trash` in the full router. Bridge has no listing of soft-deleted items.
12. **Presigned download URLs.** For large files, the full router can return a signed S3 URL — the agent reads it directly, no aidream-bandwidth. The bridge always streams through aidream.

---

## Part 3 — What's missing from the watcher we just shipped

Independent of which router we call, the watcher has gaps:

13. **Down-direction is still boundary-only.** UI edits during a session don't appear in the sandbox. `SANDBOX_LAYOUT.md` already promises "auto-syncs in both directions" — that's currently aspirational for one of the directions. **Either ship the down-direction fast or revise the doc to be honest.**
14. **Retry queue is in-memory.** Watcher crash / `/internal/reset` loses pending uploads. The shutdown bulk up-sync only catches them on a clean stop, not on a kill.
15. **Self-healing.** If `cloud-files-sync.sh down` fails (e.g. AI Dream briefly unreachable), the marker never gets written and the watcher stays dormant for the rest of the session — no periodic re-attempt. **The watcher should also be allowed to start in "degraded" mode and probe the bridge directly, not just wait for a one-way marker.**
16. **No observability.** No `/internal/cloud-sync-status`. Frontend can't show "syncing… 3 files queued, last sync 2s ago" or surface "AI Dream unreachable for 4 min".
17. **Bulk delete amplification.** `rm -rf ~/cloud-files/big-dir/` with 5,000 files = 5,000 individual DELETE calls.
18. **Multi-sandbox concurrency for the same user is undefined.** Two `:aidream` sandboxes for user U both hold local copies of `foo.txt`. A modifies → pushes v4. B (still on v3) modifies → pushes v5 with B's content as the "latest." Last-writer-wins clobber, no notification, no version-vector check. **Important: this risk gets *worse* once Realtime down-direction ships — see B1's echo-loop note.**
19. **Session-report integration.** The shutdown report doesn't include "files synced", "bytes uploaded", "errors", "p95 latency" — those would be enormously valuable for debugging. Fits cleanly into the new `~/.matrx/runtime/session-reports/` location.
20. **No `cld_events` consumer.** That table is being populated (audit trail) but no one reads it. A simple webhook worker or Realtime publication on `cld_files` would solve item 13.
21. **Cloud-sync actions aren't logged to the tool-call audit trail.** Every PUT/DELETE the watcher fires is effectively a tool action and the agent has no record of it. Folding watcher events into `~/.matrx/runtime/tool-calls/` (the `_call_logger` we just shipped) gives the agent a single place to see "what synced when" alongside its shell/python history.

---

## Part 4 — Conventions this plan must honor

Since we just standardized several things, every fix below must align with them:

- **File locations:**
  - Retry queue → `~/.matrx/runtime/cloud-sync-queue.jsonl` (JSONL so corruption only loses the last entry — never plain JSON file rewrites)
  - Status endpoint → `/internal/cloud-sync-status` on matrx_agent (port 8000)
  - Session report → `~/.matrx/runtime/session-reports/` (matches `manifest.py` pattern)
  - Per-action audit → `~/.matrx/runtime/tool-calls/<conversation_id>/<unix_ts>-cloud_sync_*-<short_id>.md` (uses the existing `_call_logger`)
- **Storage source of truth:** `sandbox_instances` rows in Postgres for sandbox lifecycle; `cld_files` rows for file metadata. The orchestrator boot-reconcile we just shipped is the precedent — every long-lived state goes to Postgres, in-memory views are caches.
- **Client abstraction pattern (matches sandbox-aware fs/shell tools):** prefer the closest, most-direct path; fall back to the broader bridge. The `BridgeClient` interface (Tier C) is the same shape as `_sandbox_proxy.py` — pluggable backend, single call site.

---

## Part 5 — The "best of best" plan (revised)

Three tiers based on priority and how surgical they are.

### Tier A — Make the existing direction bulletproof

These are all on the watcher / bridge and don't need new endpoints. ~1-2 days.

- **A1 — Persist retry queue to disk.** `~/.matrx/runtime/cloud-sync-queue.jsonl`, append-only with periodic compaction. On startup, replay any unflushed items. Mirrors `manifest.py`'s pattern.
- **A2 — Self-healing startup.** Replace the one-shot 60s wait for the down-marker with an exponential-backoff retry loop (30s → 1m → 5m, capped). The watcher is also allowed to start in **degraded mode** without the marker — tries first PUT against the bridge directly, and if that 200s, marks itself ready. Logs each retry. Watcher eventually starts when AI Dream comes back, and never gets permanently stuck because of one failed boot probe.
- **A3 — `/internal/cloud-sync-status` endpoint.** Returns `{is_running, last_sync_ts, pending, inflight, errors_recent, total_bytes, latency_p50_ms, latency_p95_ms, mode: "active"|"degraded"|"dormant"}`. Trivial wiring; the watcher already has all the inputs.
- **A4 — Splice watcher stats into the session report.** `manifest.cloud_sync = watcher.get_stats()`. The shutdown report writes to `~/.matrx/runtime/session-reports/<unix_ts>-shutdown.md` and the FE's session-report panel gains a sync section.
- **A5 — Bridge-side content-hash dedup.** In `cloud_files_bridge.py`, before calling `managed_write_async`, query `cld_files.checksum` and compare to incoming SHA-256. Match → return the existing record without bumping version. **Watcher should ALSO short-circuit by hash before sending** — the bridge dedup is the defense-in-depth layer; not sending a request you don't need is always cheaper.
- **A6 — Path-normalization at bridge entry.** Use `aidream.common.path_safety.sanitize_logical_path()` (already exists) on every incoming `file_path`. Reject paths containing `..`, normalize multi-slashes.
- **A7 — Path-traversal guard on the bulk down-sync** (`cli/files.py:211`). One-line `.resolve().is_relative_to()` check.
- **A8 — Per-action tool-call log.** Every PUT/DELETE/GET the watcher fires writes a markdown record to `~/.matrx/runtime/tool-calls/<conv>/<ts>-cloud_sync_<verb>-<id>.md` via the existing `_call_logger`. Frontmatter carries `path`, `bytes`, `version`, `latency_ms`, `success`, `cause` (`watcher`|`shutdown_bulk`|`down_sync`). Agent has a unified history; the future context-demoter can collapse them into one summary entry.

### Tier B — The other half of "auto-syncs in both directions"

This is the down-direction problem (cloud → sandbox). Multiple viable mechanisms:

- **B1 (recommended) — Supabase Realtime publication on `cld_files`.** A one-line migration: `ALTER PUBLICATION supabase_realtime ADD TABLE cld_files`. Each sandbox subscribes to `owner_id=eq.<USER_ID>`. Postgres WAL pushes events to all subscribers in <100ms. The watcher gains a `RealtimeSubscriber` that, on event, fetches the changed file via `/api/cloud-files/get` and writes it locally — but only if the local file is unchanged or older.
  - **Echo-loop guard (critical, missing from the original plan).** When the Realtime handler writes to local fs, `watchdog` will see that write and queue an upload, re-uploading the file we just downloaded. Two protections:
    1. Maintain a `recently_downloaded: dict[path, hash]` LRU. Watchdog handler skips events whose path+hash matches a recently-downloaded entry within ~30s.
    2. Hash-equality short-circuit in `_flush_path` (already in A5/scratch) catches anything that slips past.
  - **Multi-sandbox echo (item 18) gets WORSE under B1.** Sandbox A writes → cloud → Realtime → Sandbox B receives → writes locally → watchdog → upload. Without precondition checks, A and B can ping-pong forever. Acceptable for v1 because of the LRU guard; long-term fix is `If-Match: <prev_version>` precondition on bridge PUT.
- **B2 (fallback) — `GET /api/cloud-files/changes?since=<iso>` polling.** Cheap to add (one new bridge endpoint that filters `cld_files.updated_at > since`). Watcher polls every 30s. Higher latency but no Supabase config changes needed. **Use as graceful-degradation when the Realtime connection drops** (NetworkError, channel closed, etc.) — fall back to polling, log, retry Realtime every few minutes.
- **B3 (longest-term) — `cld_events` webhook worker.** AIDream already populates `cld_events`. Build a worker that tails this table and dispatches to subscribed sandboxes via WebSocket. Most flexible but most code. Probably overkill for v1.

Ship **B1 with B2 as the documented degraded-mode fallback the watcher attempts when Realtime drops.**

### Tier C — Route through `:aidream`'s local FastAPI when present

The big one. The `:aidream` variant already runs the full AIDream FastAPI on `127.0.0.1:8001` with all 45+ `/files/*` endpoints exposed. Today the watcher ignores it and round-trips to the cloud bridge.

- **C1 — Detect the local FastAPI in the watcher's startup.** Probe `127.0.0.1:8001/health`. If reachable and the `/files/*` router responds, switch the upload path to localhost. Auth via the same service token. **Codify a single `BridgeClient` interface in `cloud_sync/client.py` with two implementations** (`LocalFilesClient` for `:aidream` images, `RemoteBridgeClient` for everything else) — the watcher takes the interface, the orchestrator-style abstraction we already use for sandbox routing.
- **C2 — Use presigned uploads** (`POST /files/upload/presigned` → S3 PUT → `POST /files/finalize-upload`) for any file > 10 MiB.
  - **Credentials clarification (missing from original plan):** the presigned URL is signed by aidream's S3 creds; the sandbox just receives the URL and PUTs to it directly. **No AWS creds enter the sandbox.** This is the same model the matrx-frontend uses today for browser uploads.
  - Skips RAM buffering on the aidream side. For `:aidream` images the call goes through localhost so the URL is signed without sandbox egress; for `:core` images it goes through the public bridge but big files still skip aidream-host bandwidth.
- **C3 — Surface versioning in the SDK.** Add `mtx files versions <path>`, `mtx files restore <path> <v>`, `mtx files diff <path> <v1> <v2>` — they go through the local `/files/{id}/versions` endpoints (or fall back to the bridge if extended in A* — see below). Inside `SANDBOX_LAYOUT.md`, the cloud-files section gets a "version history is automatic — use `mtx files versions` to inspect" line.
- **C4 — Fallback to the bridge if the local FastAPI is unhealthy or we're on a `:core` image.** Single `BridgeClient` interface (introduced in C1) selects the right implementation at startup. No call site in the watcher knows which one is in use.

Why this is the "best of best": you stop having two parallel pieces of code (watcher → cloud bridge, in-sandbox aidream sitting unused for sync) and unify them — the local FastAPI is the watcher's primary backend, the cloud bridge is the fallback for `:core` images. Versioning/sharing/search visibility comes for free because the agent can call those locally.

---

## Part 6 — Out of scope (acknowledged but punted)

- **Multi-sandbox concurrency model** (item 18) — needs `If-Match` precondition + version-vector. Long-term fix.
- **Bulk-delete coalescing** (item 17) — accept many DELETE calls for now; coalesce in v2.
- **`cld_events` webhook worker** (B3) — not necessary if B1 ships cleanly.
- **Quota counting soft-deleted files** (item 6) — separate work item; covered in a `quota_v2` plan.

---

## Part 7 — Two-batch split for parallel execution

Split designed so the two agents don't step on each other's files. The seam is the `BridgeClient` interface — both batches consume it but only Batch B introduces it.

### Batch A (other agent — watcher + bridge + Realtime)

**Files owned:** `sandbox-image/sdk/matrx_agent/cloud_sync/watcher.py`, `cloud_sync/client.py` (extends — not creates the interface), `aidream/api/routers/cloud_files_bridge.py`, `aidream/common/path_safety.py`, `sandbox-image/sdk/matrx_agent/persistence/manifest.py`, the Supabase migration for the publication.

- **A1** — Persist retry queue to `~/.matrx/runtime/cloud-sync-queue.jsonl`.
- **A2** — Self-healing startup with exponential backoff + degraded-mode fallback.
- **A3** — `/internal/cloud-sync-status` endpoint on matrx_agent.
- **A4** — Watcher stats spliced into the session report.
- **A5** — Bridge content-hash dedup (and watcher short-circuit by hash before sending).
- **A6** — Path normalization at bridge entry.
- **B1** — Supabase Realtime publication + `RealtimeSubscriber` in the watcher + echo-loop LRU guard + B2 polling fallback path.

**Verification additions for Batch A:**
- After A1: kill the matrx_agent mid-upload (`kill -9`); restart; verify the queued PUT replays from `cloud-sync-queue.jsonl`.
- After A2: block egress to AI Dream (`iptables -A OUTPUT -d <ip> -j DROP`); start a fresh sandbox; verify the watcher starts in degraded mode after one failed probe rather than hanging on the marker.
- After B1: `psql … UPDATE cld_files SET … WHERE id=…` in a sibling user session; verify the change appears in the sandbox within 1s and that no echo upload is fired.

### Batch B (mine — SDK client abstraction + agent-facing tooling + audit integration)

**Files owned:** `sandbox-image/sdk/matrx_agent/cloud_sync/client.py` (CREATES the interface), `sandbox-image/sdk/matrx_agent/cli/files.py`, the existing `aidream/packages/matrx-ai/matrx_ai/tools/_call_logger.py` (extends), `SANDBOX_LAYOUT.md` (line revisions), `aidream/api/routers/files.py` (read-only — only to confirm presigned shapes; no edits).

- **C1 + C4** — Introduce `BridgeClient` interface in `cloud_sync/client.py`. Two implementations: `LocalFilesClient` (probes `127.0.0.1:8001/health`, uses `/files/*`) and `RemoteBridgeClient` (the existing `/api/cloud-files/*` path). Hand the picked instance to the watcher via constructor injection — no behavior change for Batch A's code paths until they migrate to using the interface.
- **C2** — Presigned upload path inside `LocalFilesClient` for files > 10 MiB. Uses aidream's existing `/files/upload/presigned` + `/files/finalize-upload` endpoints; no creds in the sandbox.
- **C3** — `mtx files versions/restore/diff` SDK commands. Pure SDK work; calls `LocalFilesClient` when reachable, returns "version history requires :aidream image" on `:core`.
- **A7** — Path-traversal guard on bulk down-sync (`cli/files.py:211`). Single-line fix; lives in my batch because it's adjacent to the SDK CLI work above.
- **A8** — Wire watcher action events into `_call_logger.write_tool_call_log` so PUT/DELETE/GET each generate a `~/.matrx/runtime/tool-calls/<conv>/<ts>-cloud_sync_<verb>-<id>.md` record. Tool name field uses `cloud_sync_put` / `cloud_sync_delete` / `cloud_sync_download` so the FE log filter can group them. The watcher (Batch A's territory) calls a one-line helper this batch exposes.
- **Layout doc revision** — once B1 is in flight, update the `cloud-files/` line in `SANDBOX_LAYOUT.md` so it accurately describes the bidirectional sync. Until then, soften the claim to "syncs to AI Dream's cld_files, with periodic shutdown sync until live mode lands."

**Verification additions for Batch B:**
- After C1+C4: on a `:aidream` container, log line shows `LocalFilesClient selected`; on a `:core` container, log line shows `RemoteBridgeClient selected (no local FastAPI)`. Set of fired calls is identical.
- After C2: upload a 200 MiB file from the sandbox; verify it never transits aidream's network (check aidream's nginx access log shows only the presigned + finalize calls, no body bytes).
- After A8: trigger a watcher upload; verify a markdown file appears under `~/.matrx/runtime/tool-calls/<conv>/` with `tool: cloud_sync_put` in the frontmatter. Tool-call list viewer in the FE inspector shows it alongside shell calls.

### The seam

`BridgeClient` is the contract. Batch B introduces it as:

```python
class BridgeClient(Protocol):
    async def put(self, path: str, content: bytes | AsyncIterator[bytes], *, sha256: str) -> dict: ...
    async def delete(self, path: str) -> None: ...
    async def get(self, path: str) -> bytes: ...
    async def list(self, prefix: str = "") -> list[dict]: ...
    async def versions(self, path: str) -> list[dict]: ...   # raises NotSupportedError on RemoteBridgeClient
    async def restore(self, path: str, version: int) -> dict: ...
    async def diff(self, path: str, v1: int, v2: int) -> str: ...
```

Batch A's watcher consumes whatever client gets injected. Batch B owns implementation selection + the new methods. Nothing in Batch A's territory needs to change to enable Batch B's improvements; nothing in Batch B's territory blocks Batch A's hardening work.

### Recommended sequencing

Both batches can start in parallel. Daily seam check: confirm the `BridgeClient` shape hasn't drifted. Cross-batch dependencies:

- **A8 needs the `_call_logger` extension to land first** (Batch B does it day 1, ~1 hour).
- **B1's verification (Realtime hits a real sandbox) is easier once C1 ships** because the BridgeClient logs which path it took.
- **C3's `mtx files versions` is moot on `:core`** — depends on Batch A's bridge having a versions endpoint OR the agent being on `:aidream`. Acceptable to ship `:aidream`-only first, extend bridge in a follow-up.

Total wall-clock: ~2-3 days if both batches run in parallel, ~5 days serial. The Realtime down-direction (B1) is the single biggest UX improvement; ship it as soon as A1+A2+A3 are in (the bulletproofing guarantees we don't lose data while we iterate on the new path).
