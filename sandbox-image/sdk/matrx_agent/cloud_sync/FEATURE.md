# Cloud-files sandbox replica

Verified against `watcher.py`, `client.py`, `downstream.py`, `refusals.py`,
`paths.py`, `cli/files.py`, and the AI Dream bridge on 2026-09-17.

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

**Permanent rejections are attempted once — and then HELD, never dropped.** The
watcher retries timeouts, rate limits and 5xx on the hot ladder. Other 4xx
responses are caller/policy conflicts; retrying the identical request
immediately cannot heal them and is forbidden. But a refusal is not a
completion: until 2026-09-17 the flush logged one warning and called
`queue.mark_done`, so `replay_pending` never returned the event again and the
user's edit was gone from the durable queue with nothing on any surface they
read — the file on disk still looked right, so nobody could tell.

A write the bridge would not accept — refused OR unreachable after the ladder —
now:

- stays PENDING in `cloud-sync-queue.jsonl` (a restart replays it);
- is parked in the watcher's **held** index and retried every ~5 minutes,
  jittered (`HELD_RETRY_SECONDS`) — the slow cadence, never the hot loop;
- is published on `GET /internal/cloud-sync-status` as `held_writes`, carrying
  the server's own `status`, `code`, `message` and `remedy`
  (`refusals.py::describe_bridge_failure` is the one place a failure becomes
  those four fields), plus `held_writes` and `held_last_refusal` in the session
  manifest's compact stats;
- never touches the local file. Nothing is deleted or overwritten.

A hold clears when the path is accepted, or when a newer edit to the same path
supersedes it. A 404 from a DELETE is still idempotent success (the client
swallows it), so it retires normally. `downstream.PollingSubscriber` follows the
same rule in the down direction: a 4xx on the change feed means the box has
stopped receiving cloud edits, so it is stated once with the remedy and appears
as `downstream.last_refusal` on the same status object — not one WARNING per
cycle and nothing anyone reads.

**Local blocking work stays off the event loop.** Tree hashing and per-file
hashes run through `asyncio.to_thread`. Filesystem observer callbacks hand work
to the loop with `call_soon_threadsafe` and never perform network I/O.

**There is ONE downstream transport: the bridge's change feed.** A sandbox never
subscribes to the platform database. The direct Supabase-Realtime subscriber that used to sit
beside the poller was deleted on 2026-09-17: it opened a WebSocket to the platform database
scoped only by a client-side `owner_id` filter — no organization, no bridge — behind a
five-name Supabase key ladder the orchestrator never injects (those names are on the
platform-env deny-list), so it could not run, and `make_subscriber` fell back to polling without
a word. `make_subscriber` now returns `PollingSubscriber` and LOGS which transport is in use;
the poller logs its deletion support on the first answer.

**The server sets the polling cadence; this box follows it.** Every answer from
`/api/cloud-files/changes` carries `poll_after_seconds` (mirrored in the `Retry-After` header) — 30 s normally,
45 s while AI Dream's change feed is shedding — and `downstream.PollingSubscriber` adopts it for
its next wait, jittered like every other wait here. An instruction outside 5–600 s is refused with
a warning and the built-in 30 s is kept; an answer with no instruction (an older bridge) changes
nothing. This is the fleet half of the 2026-09-14 re-sizing: 226 boxes on fixed 30 s timers arrive
at 7.5 polls/s, which is more than the server's bounded feed can serve — following the hint takes
the fleet to 5.02/s the moment anyone is shed. Server side:
`aidream/services/sandboxes/change_feed_admission.py` + knobs `infrastructure.sandbox` /
`change_feed_*`.

## The `/changes` contract — what the two halves meet on

`GET /api/cloud-files/changes?since=<iso>&limit=<n>` is the whole downstream path,
so everything the sandbox must learn has to be in its answer. The sandbox half is
implemented; the aidream half of the deletion leg is owned by the aidream lane.

Envelope:

| Key | Type | Meaning |
|---|---|---|
| `files` | list of row objects | every change with `updated_at > since`, oldest→newest |
| `next_cursor` | ISO-8601 string | the cursor to send as `since` next round |
| `deletions_supported` | bool | **whether `files` includes deletions at all** |
| `poll_after_seconds` | number (optional) | cadence instruction, honoured within 5–600 s |

Row object: `file_path` (required — a row without it is skipped), `file_size`,
`checksum`, `current_version`, `updated_at`, and for a deletion **`deleted: true`**
(`deleted_at` is accepted as the same statement, because a row carrying it can never
be a modification). The sandbox turns a marked row into `RemoteChange(kind="deleted")`,
which unlinks the local file and drops it from the hash cache; everything else becomes
`kind="modified"`.

**Why the deletion marker is not optional.** `deletions_supported` is `false` today
(`aidream/api/routers/cloud_files_bridge.py::list_changes` filters `deleted_at IS NULL`
and its docstring says soft-deletes "only show up via Realtime"). With Realtime gone,
a file the user deletes in the UI is never removed from the sandbox, and the shutdown
up-sync pushes it back to the cloud — the delete undoes itself. Until the aidream side
ships, every sandbox says so once, at WARNING, with this remedy
(`downstream.PollingSubscriber._note_deletion_support`); it is not a best-effort
silence. When aidream includes soft-deleted rows marked `deleted: true` and flips
`deletions_supported` to `true`, this image already honours it — no sandbox change, no
image rebuild ordering problem, and the sandbox's log line flips to the INFO form.

## Entry points

- `watcher.py::CloudFilesWatcher` — live bidirectional replica and durable event replay.
- `client.py::AsyncBridgeClient` — authenticated AI Dream bridge client.
- `bridge_headers.py::identity_headers` / `actor_headers` — THE ONE builder of
  the identity headers for every AI Dream call in this image, and of the
  acting-user/organization pair `client.py::SandboxClient` sends to the
  ORCHESTRATOR on heartbeat/complete/error (shell half:
  `scripts/bridge-headers.sh`). `published_identity_failure()` reads
  `/etc/matrx/bridge-env.FAILED`, the marker `write-bridge-env.sh` leaves when it
  could not publish the identity, so a wired-but-invisible box says so instead of
  looking unwired.
- `refusals.py::describe_bridge_failure` — one shape for a failed bridge call
  (status, code, message, remedy, retryable), used by the held-write surface and
  the downstream subscriber.
- `cli/files.py` — `mtx files` commands and bulk startup/shutdown safety passes.
- `scripts/cloud-files-sync.sh` — lifecycle wrapper installed in the sandbox image.

## Verification

- `pytest sandbox-image/sdk/tests/test_cloud_sync_boundaries.py`
- `pytest sandbox-image/sdk/tests/test_held_writes.py`
- `pytest sandbox-image/sdk/tests/test_orchestrator_identity.py`
- `pytest sandbox-image/sdk/tests/test_downstream_retry_after.py`
- `pytest sandbox-image/sdk/tests/test_downstream_deletions.py`

## Change log

- 2026-09-17 — **A refused write is HELD, not finished.** `_is_retryable_bridge_error`
  still judges the hot ladder, but the terminal path no longer marks the event
  done: `_hold` keeps it PENDING, parks it for a jittered 5-minute retry and
  publishes `held_writes` (with the server's status/code/message/remedy) on
  `/internal/cloud-sync-status`; the local file is never touched. The down
  direction gained `PollingSubscriber.status()` so a 4xx refusal of the change
  feed is named on the same object. Guard: `tests/test_held_writes.py` (proven
  failing-then-passing against the previous `mark_done` behaviour).
- 2026-09-17 — **The box says when it could not publish its identity.** The
  entrypoints no longer run `write-bridge-env.sh || true`; on failure the writer
  leaves `/etc/matrx/bridge-env.FAILED`, the box still starts, and
  `bridge-headers.sh` + `bridge_headers.published_identity_failure()` refuse
  loudly instead of taking the quiet "unwired image" path. Guard:
  `tests/test_cloud_sync_boundaries.py::test_no_entrypoint_swallows_the_identity_writer`
  and the marker round-trip beside it.
- 2026-09-17 — **Orchestrator signals carry the same identity.** `SandboxClient`
  heartbeat/complete/error send `X-Matrx-User-Id` + `X-Organization-Id` through
  `actor_headers`; the orchestrator refuses a mismatch (403) and reports an
  image that sends neither as unverified. Guards:
  `tests/test_orchestrator_identity.py`,
  `orchestrator/tests/test_sandbox_signal_identity.py`.
- 2026-09-17 — **One downstream transport, and deletions are a contract, not a
  hope.** The direct Supabase-Realtime subscriber and its five-name Supabase key
  ladder were DELETED (a sandbox never talks to the platform database; the bridge
  is the one hop and it carries the organization). The dead path had been
  falling back to polling silently since it shipped. `make_subscriber` now
  announces the transport, `PollingSubscriber` honours `deleted: true` rows, and
  a bridge reporting `deletions_supported: false` makes the box state the
  consequence (a deleted file is resurrected by the shutdown up-sync) with the
  remedy. Contract: § The `/changes` contract above. Guards:
  `tests/test_downstream_deletions.py`.
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
