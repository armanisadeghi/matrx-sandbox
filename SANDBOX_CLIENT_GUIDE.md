# Matrx Sandbox: Client Integration Guide

This is the authoritative reference for the orchestrator HTTP API as of orchestrator v0.2.0. It documents every route the orchestrator serves, including the proxy routes that **do not appear in `/openapi.json`** (FastAPI omits broad path catchalls from the auto-generated schema). Use [`GET /api-surface`](#0-discovering-the-surface) for machine-readable discovery — it is the source of truth.

> **History note (Apr 2026):** The cloud editor team's earlier audit of `http://54.144.86.132:8000/openapi.json` reported "only 8 endpoints exist." That was correct for the EC2 deploy at the time (which lagged the wishlist commit by 73 days), but the code on disk had already implemented the rich surface. After the v0.2.0 deploy, EC2 also exposes everything below.

---

## 0. Discovering the surface

```
GET /api-surface         → no auth required
```

Returns:
```jsonc
{
  "service": "matrx-sandbox-orchestrator",
  "version": "0.2.0",
  "tier": "ec2" | "hosted" | null,
  "routes": [
    { "path": "/sandboxes", "methods": ["POST"], "name": "create_sandbox", "kind": "http" },
    { "path": "/sandboxes/{sandbox_id}/pty", "methods": ["WS"], "name": "proxy_pty", "kind": "websocket" },
    ...
  ]
}
```

**Always read `/api-surface` rather than `/openapi.json` when discovering capabilities.** The proxy routes (`/fs/{path}`, `/git/{path}`, `/search/{path}`, `/processes/{...}`, the WS routes) only show up here.

---

## 1. Two tiers — which orchestrator am I talking to?

| Tier | URL | Backed by | Best for |
|---|---|---|---|
| `ec2` | `http://54.144.86.132:8000` | EC2 single host, S3 hot+cold, Supabase Postgres | Ephemeral agent runs, quick tasks, public-internet-only work |
| `hosted` | `https://orchestrator.dev.codematrx.com` | This dev server, Docker volumes, internal Postgres | Long-lived editor sessions, larger workloads, access to internal Matrx services |

Each orchestrator advertises its tier via `GET /` and `GET /api-surface` (`tier` field). Frontends should:

1. Persist the tier on each `sandbox_instances` row at create time.
2. Route follow-up calls (`/exec`, `/fs/*`, `/git/*`, `/extend`, etc.) to the orchestrator that hosts that tier.

A `POST /sandboxes` whose `tier` field doesn't match the orchestrator's `MATRX_HOST_TIER` is rejected with HTTP 400 — there is no cross-tier proxying.

---

## 2. Sandbox lifecycle

### Create

```
POST /sandboxes
{
  "user_id": "<uuid>",                 // required
  "tier": "ec2" | "hosted",            // optional; must match orchestrator tier when set
  "template": "bare" | "node-22" | "python-3.13",   // optional; see /templates
  "template_version": "1",             // optional
  "resources": {                       // optional; hosted tier respects, ec2 ignores
    "cpu": 2.0,
    "memory_mb": 4096,
    "disk_mb": 20480
  },
  "ttl_seconds": 7200,                 // optional override; default 7200
  "labels": { "feature": "code-editor" },  // optional free-form tags
  "config": { ... }                    // optional opaque config dict
}
```

Returns a `SandboxResponse`:
```jsonc
{
  "sandbox_id": "sbx-abc123…",
  "user_id": "...",
  "status": "ready" | "starting" | ...,
  "container_id": "...",
  "created_at": "2026-04-26T…",
  "hot_path": "/home/agent",
  "cold_path": "/data/cold",
  "config": { ... },
  "ssh_port": 32768,                   // host-mapped SSH port (when set)
  "ttl_seconds": 7200,
  "expires_at": "2026-04-26T…",
  "tier": "hosted",
  "template": "node-22",
  "template_version": "1",
  "labels": { ... }
}
```

### Read / list

```
GET /sandboxes                       → all sandboxes
GET /sandboxes?user_id=<uuid>        → just one user's
GET /sandboxes/{sandbox_id}          → one
```

### TTL extension (heartbeat ≠ extend)

Two separate concepts:

- **Heartbeat** = "I'm still here, mark me alive" (no TTL change):
  ```
  POST /sandboxes/{id}/heartbeat
  ```
- **Extend** = "Push my expiry forward" (persists `expires_at` in the DB):
  ```
  POST /sandboxes/{id}/extend
  { "ttl_seconds": 3600 }              # body
  POST /sandboxes/{id}/extend?ttl_seconds=3600   # query alt for back-compat
  ```
  Returns `{ sandbox_id, ttl_seconds, expires_at, new_expires_at }`. Range: 60–86400 seconds.

  > **Frontends:** `extend` was previously stubbed (returned 200 but didn't persist). Always check the response includes `new_expires_at` matching the requested TTL — if it doesn't, you're talking to a pre-v0.2.0 orchestrator.

### Destroy

```
DELETE /sandboxes/{id}?graceful=true  → 204 No Content
```

`graceful=true` runs `shutdown.sh` (S3 sync-back + FUSE flush on EC2; daemon-stop + ttyd kill on hosted). `graceful=false` is `docker stop --time=0`.

### Agent self-signal

Agents inside the sandbox can end their own session:

```
POST /sandboxes/{id}/complete    { "result": {...} }   # success → graceful shutdown
POST /sandboxes/{id}/error       { "error": "...", "details": {...} }   # failure → graceful shutdown
```

---

## 3. Templates

```
GET /templates
```

Returns every template variant this orchestrator can spawn:
```jsonc
{
  "templates": [
    { "id": "bare",         "version": "1", "image": "matrx-sandbox:latest", "tier": "hosted", "languages": ["python","node","bash"], "description": "..." },
    { "id": "node-22",      "version": "1", ... },
    { "id": "python-3.13",  "version": "1", ... }
  ]
}
```

Today all templates share the same Docker image; the variant is selected via the `SANDBOX_TEMPLATE` env var the in-container daemon reads at startup. The template surface is API-stable — when we add per-template Dockerfiles later, the response shape doesn't change, only the `image` field differs.

---

## 4. Buffered execution (`POST /sandboxes/{id}/exec`)

```jsonc
{
  "command": "ls -la",          // required, max 10000 chars
  "timeout": 30,                 // optional, 1-600 seconds
  "user": "agent",               // optional
  "cwd": "/home/agent",          // optional; orchestrator tracks last cwd per sandbox
  "env": {                       // optional; merged into container env for this call
    "NODE_ENV": "test",
    "DEBUG": "1"
  },
  "stdin": "..."                 // optional; bypasses the command-length cap
}
```

Returns `{ exit_code, stdout, stderr, cwd }`. The `cwd` reflects the working directory after the command ran (so `cd foo` is honored across calls).

**Limits and tradeoffs:**
- One JSON blob at the end. No streaming. Use `/exec/stream` for long-running commands.
- No cancel for the buffered path. Use `/exec/stream` (cancel via aborting the SSE connection) or `/processes/{pid}/signal`.
- 10 KB command cap — for big payloads, put them in `stdin` instead.
- For credentials that should outlive the call, use `/credentials` rather than `env`.

---

## 5. Streaming execution

```
POST /sandboxes/{id}/exec/stream    → text/event-stream
```

Body shape mirrors `/exec`. Response is an SSE stream of events for `stdout`, `stderr`, and a final `exit`. Cancellation: close the connection; the daemon sends SIGTERM to the running command after a short grace.

For higher-fidelity interactive use (vim, REPLs, ctrl-c), prefer the PTY WebSocket below.

---

## 6. File system (`/sandboxes/{id}/fs/...`)

The orchestrator proxies these straight to the in-container `matrx_agent` daemon. Paths are absolute and start with `/`. Binary content uses `encoding=base64`.

### Basic CRUD
- `GET /fs/list?path=/home/agent` — returns `{ entries: [{ name, path, kind: "file"|"dir"|"symlink", size, mtime, mode, target? }, ...] }`.
- `GET /fs/stat?path=...`
- `GET /fs/read?path=...&encoding=utf8|base64&range=0-65535`
- `PUT /fs/write` — body `{ path, content, encoding?, mode?, create_parents? }`. Atomic temp+rename.
- `POST /fs/patch` — body `{ path, edits: [{ start, end, replacement }] }`.
- `DELETE /fs/delete?path=...&recursive=true`
- `POST /fs/mkdir` — `{ path, parents? }`
- `POST /fs/rename`, `POST /fs/copy` — `{ from_path, to_path, recursive? }`

### Bulk + transfer
- `POST /fs/upload` — multipart form upload.
- `GET /fs/download?path=...&format=zip` — streams a zip of a directory.
- `POST /fs/batch` — `{ ops: [{ kind: "write"|"delete"|"mkdir"|"rename", ... }] }`. Atomic for agent-issued multi-file patches.

### Real-time watcher
- `WS /fs/watch?path=/home/agent&recursive=true&ignore=["node_modules"]`
  Server pushes `{ type: "created"|"modified"|"deleted"|"moved", path, kind, at }`. Server-debounced ≥ 50 ms.

---

## 7. Interactive terminal (PTY) and processes

### PTY WebSocket

```
WS /sandboxes/{id}/pty?cols=120&rows=30
```

- Bidirectional. Client sends raw bytes; server emits raw PTY bytes.
- Control frames as JSON text: `{ "type": "resize", "cols": 100, "rows": 40 }`, `{ "type": "signal", "name": "SIGINT" }`.
- Survives a brief disconnect (grace period). vim, nano, htop, REPLs, `git commit` all work.

### Process inspection / control

- `GET /processes` — JSON list (`pid`, `command`, `cwd`, `started_at`, `cpu_pct`, `rss_kb`).
- `POST /processes/{pid}/signal` — `{ "signal": "SIGTERM"|"SIGKILL"|"SIGINT" }`.
- `GET /ports` — listening TCP ports inside the container (informational; **not** publicly exposed). Public preview URLs are not yet implemented; tracking as a phase 3 wishlist item.

---

## 8. Git workflows

All routes proxy to the in-container daemon, which shells out to `git`. Pass `cwd` in the body when you need to scope to a subdirectory.

```
POST /git/clone     { url, dest, branch?, depth?, credentials_ref? }
GET  /git/status?cwd=...                   → { branch, ahead, behind, staged[], unstaged[], untracked[], conflicted[] }
GET  /git/diff?cwd=...&path?=...&staged?=bool
GET  /git/log?cwd=...&limit=50              → [{ sha, short, author, date, subject }]
POST /git/add        { paths[], cwd }
POST /git/commit     { message, cwd, author?, amend? }
POST /git/push       { cwd, remote?, branch?, force_with_lease? }
POST /git/pull       { cwd, remote?, branch?, rebase? }
POST /git/branch     { action: "create"|"delete"|"switch", name, cwd }
POST /git/stash      { action: "push"|"pop"|"list"|"drop", cwd, message? }
```

### Credentials

```
POST /credentials                  { kind: "github", token, scope?: "read"|"write" }
POST /credentials                  { kind: "ssh", private_key, known_hosts? }
POST /credentials/revoke
```

Configures `git config --global credential.helper` against an in-memory store with restricted permissions. Tokens never appear in `/fs/read` listings. Revoked on sandbox stop.

---

## 9. Server-side search

```
POST /search/content     { query, regex?, case_sensitive?, include_globs?, exclude_globs?, max_results? }
POST /search/paths       { pattern, max_results?, fuzzy? }
```

Streams results. Defaults exclude `node_modules`, `.git`, and binary files. Backed by `ripgrep` and `fd`.

---

## 10. SSH access

```
POST /sandboxes/{id}/access
```

Generates a one-time Ed25519 keypair, injects the public half into the container, and returns the private half + connection details. Use for direct human SSH (separate from the editor's PTY WebSocket).

---

## 11. Health and meta

| Endpoint | Auth? | Purpose |
|---|---|---|
| `GET /` | No | Service banner: `{ service, version, tier, docs, api_surface }` |
| `GET /health` | No | `{ status, active_sandboxes, uptime_seconds }` — fast liveness probe |
| `GET /system` | **Yes** | Full host pressure + container counts — see §11.1 |
| `GET /api-surface` | No | Full route list (see §0) |
| `GET /docs` | No | FastAPI auto-doc (incomplete — missing proxy routes) |
| `GET /openapi.json` | No | Same caveat as `/docs` |

All other endpoints require `X-API-Key: <key>` (or `Authorization: Bearer <key>`). The key is set via `MATRX_API_KEY` per orchestrator, separate per tier.

### 11.1 `GET /system` — host pressure for the admin panel

```jsonc
{
  "tier": "ec2" | "hosted" | null,
  "uptime_seconds": 3621.4,
  // Disk (root FS — same volume as orchestrator code + sandbox volumes)
  "disk_total_bytes": 53687091200,
  "disk_used_bytes": 13408000000,
  "disk_free_bytes": 40279091200,
  "disk_used_pct": 25.0,
  // Memory (kB granularity, from /proc/meminfo)
  "memory_total_kb": 16312828,
  "memory_used_kb": 3104260,
  "memory_available_kb": 13208568,
  "memory_used_pct": 19.0,
  // CPU
  "cpu_count": 4,
  "load_1m": 0.42, "load_5m": 0.31, "load_15m": 0.27,
  // Sandbox counts — both views, so operators can spot drift
  "sandboxes_in_db": 12,
  "sandboxes_active": 4,
  "sandbox_containers_total": 4,
  "sandbox_containers_running": 4
}
```

Powers the **Sandbox Infrastructure** admin panel in matrx-frontend (`/administration/sandbox-infra`). When `sandboxes_active != sandbox_containers_running`, the orchestrator's view of the world has drifted from Docker's — investigate (usually the orchestrator restarted but the store didn't reconcile).

---

## 12. Out of scope (deferred wishlist items)

These are recognized needs but are not implemented in v0.2.0:

- **Public preview URLs** (`POST /ports/expose`) — needs Traefik dynamic config per sandbox port.
- **Snapshot / resume** — needs CRIU or VM-based runtime.
- **Multi-user shared sandboxes** — needs permission model + shared PTY semantics.
- **LSP proxy** — language servers in the image + Monaco LSP wiring.
- **AI model integration sockets** — depends on a concrete model strategy.
- **Background exec + log retrieval** (`/exec/background`) — PTY covers most cases short-term.

See [SANDBOX_API_WISHLIST.md](SANDBOX_API_WISHLIST.md) §3.3 for design notes.

---

*Reference. Update whenever the wire shape changes — `/api-surface` is the runtime source of truth, this doc is the human-friendly companion.*
