# cldfs — cloud-files as a live filesystem inside the sandbox

> **Status:** design + scaffolding (2026-05-27). Phase 6b of the
> compute-targets / cloud-files unification work. The Phase 6a "lazy load +
> Realtime invalidation" (env-flag-gated in `cloud-files-sync.sh`) is the
> bridge while cldfs is built out.

## Why

Today's `~/cloud-files/` is a **copy** of the user's `cld_files`:

1. Eager `aws s3 sync` style pull at boot (every sandbox waits)
2. Local writes get pushed back to `cld_files` via a watchdog (~5s lag)
3. UI edits during a session **don't appear** in the sandbox until next boot

That made sense when S3 was cross-region. Now that S3 + EC2 orchestrator +
sandbox containers all run in the same AWS region, the boot copy is wasted
work and the freshness gap is fixable. cldfs replaces the copy with a live
FUSE view of `cld_files`.

## Goals (in priority order)

1. **What you see in the AI Dream UI is what you see in the sandbox** — zero
   propagation delay either direction.
2. **No boot tax** — opening a sandbox doesn't wait for an N-GB sync.
3. **No double-storage** — bytes live in S3 once.
4. **Versioning intact** — writes from the sandbox still produce proper
   `cld_files` versions; `mtx files versions <path>` keeps working.
5. **Graceful degrade** — if cldfs can't reach the bridge or S3, reads fall
   back to a local read-through cache; writes queue and replay.

## Non-goals

- Replacing `/home/agent/` (the agent's local SSD scratch). cldfs is mounted
  at `/home/agent/cloud/`; tools that need POSIX semantics (`git`, `npm`,
  `cargo`) still work on `~/` for working state.
- Cross-region operation. cldfs assumes same-region S3.
- Read of files the user doesn't own (RLS on `cld_files` is still the gate).

## The trap with "just mount the S3 bucket"

`cld_files` paths are **logical**. The actual S3 keys are content-addressed
or version-keyed (`{file_id}/{version}/blob` per `cld_files.storage_uri`).
You can't `mountpoint-s3` the cloud-files prefix and get the user's tree —
versioning, soft-delete, sharing, and the path → blob mapping all break.

cldfs solves that by being a **purpose-built FUSE handler** that talks to
`cld_files` for path resolution and S3 (or the bridge) for byte fetch.

## Architecture

```
                                     ┌─────────────────────────┐
                                     │  AI Dream FastAPI       │
                                     │  /api/cloud-files/*     │
                                     │  (already shipped)      │
                                     └─────────┬───────────────┘
                                               │
                                               │  REST  (writes, large reads)
                                               │
┌──────────────────────────────────────────────┼────────────────────────────┐
│ Sandbox container                            │                            │
│                                              ▼                            │
│   /home/agent/cloud/   ◀── FUSE ──   cldfs.handler                        │
│   (mount point)                          │                                │
│                                          ├──▶  SQLite mirror              │
│                                          │    of cld_files                │
│                                          │    (path, file_id, size, etag, │
│                                          │     current_version, mtime)    │
│                                          │                                │
│                                          ├──▶  Supabase Realtime          │
│                                          │    INSERT/UPDATE/DELETE        │
│                                          │    on cld_files filtered by    │
│                                          │    owner_id=eq.$USER_ID        │
│                                          │                                │
│                                          └──▶  S3 GET (same region)       │
│                                               for read bytes              │
└────────────────────────────────────────────────────────────────────────────┘
```

## File lifecycle

### Read

1. Agent opens `/home/agent/cloud/case-2026-Q1/brief.pdf`.
2. FUSE `lookup` → SQLite query: find row where `file_path = "case-2026-Q1/brief.pdf"`.
3. FUSE `getattr` → return size / mtime from SQLite.
4. FUSE `open` → check local byte-cache (`/tmp/cldfs-cache/{file_id}-v{N}`).
   - Hit: serve from local cache.
   - Miss: GET via `/api/cloud-files/get?path=...` (bridge streams from S3),
     write to cache, then serve.
5. Subsequent `read(fd, offset, n)` → from cached buffer.

### Write

1. Agent opens `/home/agent/cloud/notes/today.md` for write.
2. FUSE `create` / `open(O_WRONLY)` → allocate a staging file
   (`/tmp/cldfs-staging/{path}.tmp`).
3. Writes append to the staging file, no network calls.
4. FUSE `flush` / `release` → POST `/api/cloud-files/put` with the staging
   file's bytes. Bridge creates a new `cld_files` version. The SQLite mirror
   updates from the Realtime event (or from the bridge response if Realtime
   is down).
5. Staging file is moved into the byte-cache so the next read returns the
   freshly-written bytes without a round-trip.

### Delete

1. FUSE `unlink` → DELETE `/api/cloud-files/delete?path=...` (soft-delete in
   `cld_files`).
2. SQLite mirror updates from Realtime, the entry vanishes from the next
   `readdir`.

### Rename

1. FUSE `rename` → POST `/api/cloud-files/move?from=X&to=Y` (new bridge
   endpoint — see API additions below).
2. Bridge updates `cld_files.file_path` in a transaction.

### External change (web UI uploaded a file)

1. Realtime delivers `INSERT` event for `cld_files` row.
2. SQLite mirror inserts the row.
3. FUSE `readdir` on the parent dir now returns the new file.
4. Optional: pre-warm the byte cache by fetching the bytes on the Realtime
   event (lazy alternative: wait for first `open`).

## Local mirror schema

`/var/lib/cldfs/mirror.db` (or `~/.cldfs/mirror.db` for unprivileged tier):

```sql
CREATE TABLE files (
  file_id        TEXT PRIMARY KEY,         -- cld_files.id
  file_path      TEXT NOT NULL UNIQUE,
  file_size      INTEGER NOT NULL,
  mime_type      TEXT,
  current_version INTEGER NOT NULL,
  etag           TEXT,                     -- cld_files.checksum / S3 ETag
  mtime          REAL NOT NULL,            -- from cld_files.updated_at
  owner_id       TEXT NOT NULL
);

CREATE INDEX idx_files_path ON files(file_path);
CREATE INDEX idx_files_dir  ON files(substr(file_path, 1, instr(file_path, '/')));
```

Populated at boot from `GET /api/cloud-files/list`, then kept current by the
Realtime subscriber. Mirror is rebuilt lazily on cache miss if the row isn't
found.

## API additions (bridge)

Existing `/api/cloud-files/*` covers list, get, put, delete, quota.
cldfs needs one more endpoint to make `rename` atomic:

```
POST /api/cloud-files/move
Body: { from_path: "old/a.txt", to_path: "new/a.txt" }
Response: { id, from_path, to_path, version }
```

Today an agent has to `get` + `put` + `delete` to move. cldfs can paper over
this with the three-step fallback when the endpoint is missing, but adding
`/move` lets us preserve versioning and audit cleanly.

## Trade-offs

**Wins**

- Boot time: drops from "30s waiting for sync" to "instant" for users with
  large file trees.
- Disk: each sandbox has only the bytes the user reads (cache), not the
  whole tree.
- Freshness: zero lag both directions (Realtime down → polling at 30s; both
  better than the current "next boot" gap).
- Storage cost: one set of bytes in S3, period.

**Costs**

- FUSE/S3 doesn't give POSIX semantics for some operations. `rename` is
  atomic via the bridge, but `flock`, hard links, sparse-file holes, and
  fast random writes are best-effort. The agent should not `git clone`
  INTO `/home/agent/cloud/`; clone to `/home/agent/work/` and copy outputs
  in. We document this and lint for it in the agent's prompt.
- A "rm -rf" footgun: a user typo in the sandbox could nuke their cloud
  files. Mitigated by:
  - `cld_files` soft-delete is the default — recoverable.
  - Versioning means even an overwrite is recoverable via `mtx files restore`.
  - Optionally an "extra prompt before bulk delete" mode (`MATRX_CLDFS_GUARD=strict`).
- Write semantics: we commit on `release()`, not on every `write()`. A long
  open-write-fsync-loop won't produce a new version per fsync. That matches
  cld_files versioning intent (commits, not edits).

## Phasing

- **Phase 6a (shipping now):** `MATRX_CLOUD_FILES_LAZY=1` env flag skips the
  eager boot copy. The existing `CloudFilesWatcher` + `RealtimeSubscriber`
  fill in mid-session changes. `~/cloud-files/` still exists as a directory,
  just lazy-loaded.

- **Phase 6b (this skeleton, future production):** cldfs FUSE at
  `~/cloud/`. Coexists with `~/cloud-files/`; entrypoint picks one based on
  `MATRX_CLDFS_MODE` (`off` | `bridge` | `fuse`).

- **Phase 6c (later):** retire `~/cloud-files/` directory after a soak
  period. Single mount, single source of truth, single piece of code to
  maintain.

## Operator checklist for Phase 6b rollout

1. Image must include the `pyfuse3` package (currently NOT in the SDK
   pyproject; add when ready to ship cldfs). On Linux installs as
   `libfuse3-3` + `pyfuse3` wheel.
2. Container needs `--cap-add SYS_ADMIN --device /dev/fuse` (already in
   `sandbox-local/docker-compose.yml` for cold-mount; same flags work).
3. `MATRX_CLDFS_MODE=fuse` env var must be passed through by the
   orchestrator.
4. `SUPABASE_URL` + `SUPABASE_ANON_KEY` must be in the container env
   (already passed through per `orchestrator/config.py`).
5. Apply migration `db/migrations/0002_cld_files_realtime.sql` to the
   Supabase project (already applied as of 2026-05-27).
6. Add `POST /api/cloud-files/move` to the AI Dream bridge router for
   atomic rename support.

## Files

The skeleton lives in [sandbox-image/sdk/matrx_agent/cldfs/](../sandbox-image/sdk/matrx_agent/cldfs/):

- `__init__.py` — public exports (`mount()`, `Config`)
- `handler.py` — FUSE operations (lookup, getattr, readdir, open, read, write, release, unlink, rename)
- `mirror.py` — SQLite-backed metadata mirror
- `cache.py` — local byte cache (read-through, LRU eviction)
- `staging.py` — write staging + commit-on-release
- `mount.py` — entrypoint helper / CLI

The Python skeleton intentionally compiles without `pyfuse3` so the rest of
the SDK can ship without the FUSE dep until cldfs is production-ready. The
FUSE bindings are imported inside `mount()`.
